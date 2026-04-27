## Goals

1. Stop emitting the wrong-shaped worker commit (Finding 2 from
   the 2026-04-26b smoke triage). Turma owns bd state at the
   commit boundary; bd's misbehaving pre-commit hook is
   bypassed locally for that one commit.
2. Keep operator-driven bd reads between iterations from
   refusing the next preflight (Finding 1). `export.interval=0`
   is the contract; Turma verifies it before each run.
3. Preserve everything the prior arcs got right: the
   revert-after-mutation contract, the bd-state preflight on
   main's working tree, the tail-mutation warning, the
   chained-flow correctness.

## Non-goals

- Fix the upstream bd defect. The reproducer is drafted for
  filing; the fix is not on this PR's path.
- Touch any code outside the worker-commit step or the
  preflight. The orchestrator's main loop, repair phase,
  reconciliation, and merge-advancement remain unchanged.
- Provide a fallback for operators who refuse to set
  `export.interval=0`. Refusing is the contract; the operator
  triage commands in the preflight error message guide them
  to fix.

## Empirical model behind the design

Two findings, two failure paths, two corresponding fixes.
The exact mechanisms — pinned in
`docs/upstream-bd-worktree-precommit-bug.md` and the smoke
triage — are:

### Finding 1 timeline (read-side)

```
T0: turma run iter-1 finishes; revert-after-mark_pr_open ran;
    main's working tree clean. .beads/issues.jsonl matches HEAD.
T1: operator runs `bd list --label feature:smoke-merge --json`
    (verification command from the runbook).
    bd checks: throttle window elapsed since last export?
    If yes, fire the pending export to .beads/issues.jsonl.
T2: main's working tree dirty. Next `turma run` preflight
    correctly refuses.
```

`export.interval=0` collapses T1 because there's no pending
export to flush — every bd write at iter-1 already exported
immediately, and the revert cleaned each one.

### Finding 2 timeline (worker-commit)

```
T0: Turma sets up worktree at .worktrees/<feat>/<id> on branch
    task/<feat>/<id> from main. Worktree's HEAD (= main's HEAD)
    has .beads/issues.jsonl tracked.
T1: ClaudeCodeWorker.run spawns claude with cwd=worktree.
    claude's SessionStart hook auto-fires `bd prime`.
    `bd prime` lazy-inits the worktree's .beads/embeddeddolt/
    AND silently removes .beads/issues.jsonl from the working
    tree. (bd does NOT log this removal.)
T2: claude makes its task-file Write call(s) and writes
    .task_complete. Exits.
T3: Turma's commit_all runs `git -C <worktree> add -A` →
    stages: D .beads/issues.jsonl, A <task-file>.
T4: Turma's commit_all runs `git -C <worktree> commit -m ...`.
    git fires bd's pre-commit hook (`timeout 300 bd hooks run
    pre-commit`).
T5: bd's pre-commit hook calls `bd export` + bd's git-add layer.
    The export's path resolves to the worktree's repo ROOT
    (issues.jsonl), NOT .beads/issues.jsonl. bd stdout still
    says "Exported N issues to .beads/issues.jsonl" — the
    log is wrong.
T6: Commit captures: D .beads/issues.jsonl, A <task-file>,
    A issues.jsonl (at root). PR opens with this content.
    Squash-merge propagates the wrong shape into main's HEAD.
```

The new design intercepts at T3–T4: replace bd's hook-driven
export+stage at T5 with a Turma-driven explicit export at the
correct path BEFORE `git add -A`, then commit with the hook
disabled.

## The new commit-boundary protocol

The export Turma performs in this protocol is **commit-boundary
state**, not worker state. It runs once, immediately before the
worker commit lands, exactly because that is the one moment the
broken upstream hook would otherwise inject corrupt state. The
implementation must not drift into "sync bd whenever convenient"
shapes (pre-task, post-task, mid-loop). Any future use case
that wants more bd-state syncing should propose its own arc;
this one stays scoped to the commit boundary.

`GitAdapter.commit_all` (today: `git add -A` then `git commit
-m <msg>`) is replaced with this five-step sequence per worker
commit:

```
# Inputs:
#   repo_root  : main's repo root (where Turma's bd db lives)
#   worktree   : per-task worktree path
#   message    : commit message

1. bd export -o <worktree>/.beads/issues.jsonl
   - cwd: repo_root
   - reason: main's bd db has Turma's claim_task / mark_pr_open
     mutations. The worktree's bd db (per Task 0 of the prior
     arc and probes in 2026-04-26b) is a separate, lazily-
     populated db that does NOT share main's mutations.
     Exporting from main's cwd captures the canonical state.
   - failure: PlanningError("bd export failed: exit <N>\n
     <stderr>"). bd's stderr passes through verbatim.

2. (no separate "ensure file exists" step needed: step 1
    already creates the file at the right path.)

3. git -C <worktree> add -A
   - argv unchanged from today's commit_all.
   - now stages: A .beads/issues.jsonl (or M, if HEAD already
     had the file with different content), A <task-file>,
     plus the deletion of any sentinel files that aren't
     gitignored.

4. git -C <worktree> -c core.hooksPath=/dev/null commit -m <msg>
   - hook bypass scoped to this single invocation, NOT a
     global config change. bd's prepare-commit-msg / post-merge
     etc. are unaffected outside this command.
   - reason: bd's pre-commit hook misroutes the export path
     when fired from inside a worktree primed by `bd prime`.
     We've already produced the canonical export at step 1;
     bd's hook would only re-do the work and corrupt the
     path.
   - status_is_dirty refusal in commit_all stays — it still
     guards against an empty commit.

5. (existing) parse `git rev-parse HEAD` for the commit SHA;
    return as today.
```

### Why the export comes from main's cwd, not the worktree's

The 2026-04-26b probes empirically established:

- Plain `bd update <id>` invoked from inside a worktree writes
  to `.beads/issues.jsonl` correctly — bd's worktree path
  resolution works for direct writes.
- Plain `bd hooks run pre-commit` invoked from inside a
  worktree writes to `.beads/issues.jsonl` correctly — when
  fired against a clean index.
- BUT when bd's pre-commit hook fires against an index with
  `D .beads/issues.jsonl` (the state `bd prime` left), the
  export's stage-side path resolution wrongly drops the
  `.beads/` prefix.
- AND the worktree's bd db (when bd prime has run) reports
  "Exported 0 issues" — the worktree's db is empty even when
  main's db has the issues. So an export from inside the
  worktree would write an empty file even if the path
  resolution were correct.

Running the export from main's cwd sidesteps both issues.
Main's bd db is the canonical source. The output path is
absolute (the worktree's `.beads/issues.jsonl`), so cwd
ambiguity doesn't matter for the destination.

### Failure behavior — hard fail before commit

Each step in the protocol is an independent failure boundary;
none degrade. If the export step (1) fails, the commit MUST
NOT run — `commit_all_with_bd_export` raises `PlanningError`
and propagates up through `_run_single_task`'s normal
failure path. Specifically:

- bd export non-zero exit → `PlanningError("bd export failed:
  exit <N>\n<stderr>")`. No `git add`, no `git commit`. The
  worktree is left in whatever state it was before the call;
  the orchestrator's existing failure path handles cleanup
  (sentinel detection, `_handle_failure`, etc.).
- bd export returns zero but the destination file does not
  exist at `<worktree>/.beads/issues.jsonl` afterwards →
  `PlanningError("bd export reported success but destination
  path is missing: <path>")`. This catches the upstream-fix
  edge case where bd's path resolution lands the file
  somewhere else and exits zero.
- `git add -A` non-zero exit → `PlanningError`, no commit.
- `git commit ... -c core.hooksPath=/dev/null` non-zero
  exit → `PlanningError`, propagates the existing
  empty-commit guard.

The contract is binary: the worker commit either reflects
all four steps (export ran AND staged AND committed
successfully) or it doesn't run. There is no partial-commit
intermediate state where some files are staged but the
export wasn't.

### Why hook bypass alone is not enough

Hook bypass without an explicit export would leave the
worktree's `.beads/issues.jsonl` deleted (because `bd prime`
removed it earlier in the worker run). The commit would then
capture the deletion — same wrong-path-adjacent corruption,
just missing the wrong-path part. The worker-commit
propagation contract requires the file to be present at the
right path with main's bd state at commit time.

## The export.interval=0 Turma contract

Where verified: in `_preflight_beads_state_clean(services)`
or a new sibling helper. Either:

(a) read `bd config get export.interval` → expect "0"; refuse
    on any other value with a typed message;
(b) verify it via `bd config show` parsing.

Either implementation is fine — the spec pins the
contract, not the bd CLI subcommand. Test pin uses a stub
that returns the value Turma asks for.

The check runs alongside the existing dirty-bd-state
preflight, BEFORE `fetch_and_ff_base`, AND is **skipped
under `--dry-run`** (consistent with the existing
preflight).

The error message is operator-actionable:

```
turma run requires `bd config get export.interval` to
return 0 (default is 60). The 60s throttle defers bd's
auto-export across iterations; with the throttle in
place, any bd command (including reads like `bd list
--json`) between turma runs re-dirties
.beads/issues.jsonl and the next preflight refuses.

Run:
  bd config set export.interval 0

(this is committed to .beads/config.yaml; one-shot per
project).
```

## Tradeoffs explicitly accepted

- **Bypass is hook-scoped, not all-bd-side.** Turma still
  runs bd commands (claim_task, mark_pr_open, etc.) from
  main's repo root — those continue to fire bd's pre-commit
  hook on Turma's behalf when Turma later commits.
  But Turma's flow does NOT commit in main's working tree
  during `turma run` (only worker-side worktrees and the
  squash merge from gh do that), so the hook bypass is only
  needed in the one place this spec touches.
- **bd's export semantic is now Turma's contract.** If
  upstream bd ships a fix that re-routes the hook export to
  `.beads/issues.jsonl` correctly, Turma's explicit export
  becomes redundant overhead. The cost is one bd subprocess
  per worker commit. We accept this — a redundant export is
  cheap and harmless; the alternative (depending on a
  defect-fixed upstream we don't control the timeline of) is
  not.
- **`export.interval=0` is mandatory, not optional.** An
  operator who refuses gets a refusing preflight with a
  clear remediation path. We accept the friction in
  exchange for closing the read-side dirtying gap once.

## What stays unchanged

- `revert_paths`, `path_is_dirty`, the 6 callsite reverts
  in the orchestrator: all unchanged.
- The dirty-bd-state preflight on main's working tree:
  unchanged (one new sibling preflight added; the existing
  one stays).
- The tail-mutation warning at end of `run_swarm`:
  unchanged.
- `fetch_and_ff_base`: unchanged.
- Worker-side `claim_task`, `mark_pr_open`, `unmark_pr_open`,
  `close_task`, `fail_task`: unchanged. Turma still mutates
  bd's main db at the same call sites; revert still cleans
  main's working tree after each.
- Worker prompt template: unchanged.
- ClaudeCodeWorker.run argv: unchanged (`claude -p <prompt>
  --dangerously-skip-permissions`, cwd=worktree).
- WorktreeManager: unchanged (plain `git worktree add ...`).

## Open questions / future work

- **Should Turma own MORE bd hooks proactively?** This arc
  bypasses only pre-commit and only at the worker-commit
  boundary. If post-merge or prepare-commit-msg defects
  surface later, the same pattern (Turma-driven explicit
  state ownership + scoped hook bypass) is the obvious
  template. Out of scope for v1.
- **Should `bd config get export.interval` be set by
  `turma init` automatically?** Possibly cleaner than
  refusing in preflight. But `turma init` is a separate
  command and changing operator-visible behavior there
  needs its own design pass. v1 ships the preflight refusal
  with a clear remediation; a future arc can move the set-
  on-init if operator feedback warrants it.
