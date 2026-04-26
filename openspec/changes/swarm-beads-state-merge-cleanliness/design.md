## Scope

Narrow correction following the 2026-04-26 chained-flow
smoke iter-2 failure. Establishes that Turma owns the
working-tree state of `.beads/issues.jsonl` on main and
must revert that file after each Turma-initiated bd
mutation. Keeps bd's dolt-as-source-of-truth design intact;
keeps `claim_task` / `mark_pr_open` running where they
already run.

Out of scope:

- Moving any bd mutation point to a different cwd. The
  prior draft's "claim runs in the worktree" direction
  is explicitly retired (see "Wrong-direction post-
  mortem" below).
- Multi-operator concurrent claims and push-only-via-PR
  workflows. (For shareability semantics this arc DOES
  affect, see "Shareability contract: what this arc
  actually changes" below.)
- The deferred orphan-branch decision still parked.

## What the smoke surfaced

After iter-1 of a chained `turma run`, main's working
tree was dirty:

```
$ git status
On branch main
Your branch is behind 'origin/main' by 1 commit, ...

Changes not staged for commit:
        modified:   .beads/issues.jsonl
```

Iter-2's `merge --ff-only origin/main` refused. The fetch
step succeeded; the merge step's working-tree-protection
check is what blocked the run.

Tracing the dirtiness:

1. iter-1 `_run_single_task`:
   - `claim_task(A)` → `bd update <id> --claim` runs
     subprocess from cwd=main_repo. Bd updates the dolt
     db. Bd's post-update hook re-exports
     `.beads/issues.jsonl`. Working tree dirty.
   - `setup_worktree`, `run_worker`, etc. → no further
     mutations on main's working tree.
   - `mark_pr_open(A, N)` → another `bd update` from
     cwd=main_repo. Dolt db updated. Hook re-exports.
     Working tree dirtier.
2. iter-1 ends with main's working tree carrying these
   uncommitted changes to `.beads/issues.jsonl`.
3. iter-2 starts. `fetch_and_ff_base` runs symbolic-ref
   ✓, fetch ✓, merge --ff-only origin/main → refused.

## The architectural model that fixes this

bd's `.beads/.gitignore` (committed by `bd init`):

```
# Dolt database (managed by Dolt, not git)
dolt/
embeddeddolt/
...
```

`.beads/embeddeddolt/` (the dolt database) is **gitignored
and local-only**. The only tracked bd-state file is
`.beads/issues.jsonl`, which bd's hooks rewrite as a
text export on every `bd update`.

bd locates `.beads/` by walking up from cwd. From inside
a git worktree, walking up traverses the worktree root.
**Empirically (Task 0, 2026-04-26)**: the worktree has
its own `.beads/` directory checked out from main's
HEAD, with `embeddeddolt/` initially absent (gitignored,
not checked out). bd from inside the worktree walks up
and finds the WORKTREE's `.beads/`, not main's, and
lazily creates an `embeddeddolt/` there on first
invocation. **Each worktree has its own bd db**, not a
shared one.

(An earlier draft of this design assumed shared-db-via-
cwd-walkup. That was empirically wrong. See "Wrong-
direction post-mortem" for what changes when you
correct that mental model.)

What matters for THIS arc's correctness is narrower:
Turma's `turma run` and `turma status` invoke bd
commands only from main's working tree. They never run
bd from a worktree. So the relevant claim is about
main's bd db, not a hypothetical shared db.

Implication for `turma status` and `turma run`
reentrancy:

- `bd ready`, `bd list`, `bd show` invoked from main's
  working tree read main's dolt db. Their output
  reflects the latest Turma mutation immediately.
- `turma status` (which always runs from the operator's
  shell on the main checkout) → main's bd db → sees
  in-flight state.
- A second `turma run` invocation from main → main's
  bd db → sees the in_progress task and won't re-claim.

Worker-side propagation (worker's pre-commit hook firing
inside a worktree, exporting bd state to the worktree's
`.beads/issues.jsonl`, getting captured in the worker's
commit) is handled by **bd's own internal worktree-
handling logic**, which Turma does not model. Empirical
verification: the 2026-04-26 smoke iter-1 worker commit
captured the post-`claim_task` `in_progress` state
correctly (`claim_task` ran from main; worker's
pre-commit hook in the worktree exported a matching
state). Whatever bd does to keep these aligned across
the main↔worktree boundary, it works for v1's purposes.

So the bd state being mutated by `claim_task` (in main's
db) is exactly what we need for status + reentrancy.
The problem is purely in the working-tree state of
`.beads/issues.jsonl` on main, which is a derived
export bd's hook rewrites + auto-stages on every bd
update.

## Adapter contract

`GitAdapter` gains one new method:

```python
def revert_paths(
    self, repo_root: Path, paths: tuple[str, ...]
) -> None:
    """Restore `paths` in `repo_root`'s working tree AND
    index to the HEAD version, discarding both staged
    and unstaged changes.

    argv: `git -C <repo_root> restore --staged --worktree
            -- <p1> <p2> ...`.

    Empty paths is a no-op (no subprocess call). Failure
    surfaces as `PlanningError` preserving stderr.
    """
```

**Why both `--staged` and `--worktree`**: bd's
`export.git-add=true` config (default true, verified
empirically in Task 0) means `bd update` doesn't just
write `.beads/issues.jsonl` — it also `git add`s it.
After a `bd update`, the file is dirty in the index AND
the working tree. A simple `git checkout -- <path>`
(which only restores from index) would leave the index
still dirty; the next `merge --ff-only` would still
refuse. `git restore --staged --worktree` clears both.

(An earlier draft of this design said `git checkout --
<path>`. That was wrong — see "Wrong-direction
post-mortem" for why and what Task 0 caught.)

`git restore --staged --worktree` requires git 2.23+.
The smoke runbook's existing `--initial-branch` flag
on `git init` requires git 2.28+, so 2.23 is a strict
subset of what Turma already requires. Safe.

`BeadsAdapter` is **unchanged**. No `cwd` param. No new
methods.

## Preflight: bd-state cleanliness

Turma's "I own the working-tree state of
`.beads/issues.jsonl`" claim is only safe if the file is
clean before Turma starts mutating. If the operator has
pre-existing uncommitted changes — from a manual `bd
update`, a crashed prior `turma run`, or hand edits —
unconditionally reverting after the first Turma mutation
would silently destroy those changes.

The fix: a preflight check at the top of `run_swarm`,
before `fetch_and_ff_base`, that fails fast if
`.beads/issues.jsonl` is dirty in the working tree.

```python
def _preflight_beads_state_clean(services: SwarmServices) -> None:
    """Refuse to start if Turma's owned bd-state file
    is already dirty. Turma's revert-after-mutation
    invariant only holds from a clean baseline; pre-
    existing operator changes must be triaged before
    Turma takes ownership.
    """
    if services.git.path_is_dirty(
        services.repo_root, ".beads/issues.jsonl"
    ):
        raise PlanningError(
            ".beads/issues.jsonl has uncommitted changes "
            "in main's working tree. turma run requires "
            "this file to be clean before starting "
            "because it manages the file's working-tree "
            "state across iterations. Triage with:\n"
            "  git diff --cached .beads/issues.jsonl    # staged\n"
            "  git diff .beads/issues.jsonl             # unstaged\n"
            "  git stash push -- .beads/issues.jsonl    # save aside\n"
            "  git restore --staged --worktree -- .beads/issues.jsonl    # discard"
        )
```

`GitAdapter` gains a small companion to `revert_paths`:

```python
def path_is_dirty(self, repo_root: Path, path: str) -> bool:
    """True if `path` has uncommitted changes (modified,
    staged, or both) in `repo_root`'s working tree.

    argv: `git -C <repo_root> status --porcelain=v1
            -- <path>`.

    Returns False if the path is clean OR not tracked
    (untracked file appears in porcelain output with `??`
    prefix; we don't refuse on that — operator's untracked
    file at that path is none of Turma's business).
    """
```

The check fires:

- Always in non-`--dry-run` runs, before
  `fetch_and_ff_base`.
- **Skipped under `--dry-run`** because dry-run doesn't
  mutate bd state (`_apply_repairs` and `_main_loop` are
  skipped; `_advance_merged_prs` only reads PR state).
  A dry-run readout against a dirty bd-state file is
  safe — Turma won't revert anything in dry-run mode.

This places the safety boundary at the right layer:
**Turma fails to start** rather than starting and then
discovering destruction mid-flow. The error message
gives operators the three concrete commands they need
to triage (diff, stash, or discard).

The check is on `.beads/issues.jsonl` specifically. bd's
post-update hook only writes that file (verified
empirically; bd's other tracked files —
`config.yaml`, `metadata.json`, `hooks/`, `README.md`
— don't change on `bd update`). If a future bd version
expands its hook output, this check needs broadening.
Documented; out of scope for v1.

## Orchestrator wiring

A small private helper in `_orchestrator.py`:

```python
_BEADS_EXPORT = (".beads/issues.jsonl",)

def _revert_beads_export(services: SwarmServices) -> None:
    """Revert main's working-tree state of
    `.beads/issues.jsonl` after a Turma-initiated bd
    update. The dolt db (the source of truth) keeps the
    mutation; the export file is regenerable.
    """
    services.git.revert_paths(services.repo_root, _BEADS_EXPORT)
```

Five callsite changes in `_orchestrator.py`:

| Where | bd mutation | Add after |
| --- | --- | --- |
| `_run_single_task`, top | `claim_task` | `_revert_beads_export` |
| `_run_single_task`, success tail | `mark_pr_open` | `_revert_beads_export` |
| `_run_single_task`, failure path | `fail_task` | `_revert_beads_export` |
| `_apply_repairs`, `CompletionPendingWithPr` arm | `mark_pr_open` | `_revert_beads_export` |
| `_advance_merged_prs`, MERGED dispatch | `unmark_pr_open` + `close_task` | `_revert_beads_export` (single revert after both) |
| `_advance_merged_prs`, CLOSED dispatch | `unmark_pr_open` + `fail_task` | `_revert_beads_export` (single revert after both) |

(The MERGED and CLOSED arms call two bd mutations
back-to-back; one revert at the end of each arm is
sufficient.)

The helper is small enough to inline at each callsite, but
keeping it factored as `_revert_beads_export` makes the
"Turma owns this file" decision visible in one place.

## Why this is not a path-3 workaround

The reviewer rejected an earlier draft's auto-stash
approach in `fetch_and_ff_base` as "a Git-symptom
workaround that ages badly." The ownership distinction:

- **Path 3 (rejected)**: `fetch_and_ff_base` stashes
  whatever's dirty before merge, restores after. The
  adapter has no semantic knowledge of what's dirty or
  why; it bulk-stashes. Brittle on conflict, untargeted,
  hides ownership in a low-level adapter.
- **This proposal**: the orchestrator reverts ONE named
  file after each KNOWN mutation. The orchestrator
  knows it just ran `bd update`; it knows that triggered
  bd's hook to rewrite `.beads/issues.jsonl`; it knows
  the export is regenerable from the dolt db. The
  revert restores a known invariant, not a guess.

`GitAdapter.revert_paths` is general (no special
knowledge of `.beads/`); the bd-specific knowledge lives
at the orchestrator callsite. If a future arc adds a
sweep-phase mutation in a new code path, it needs to
explicitly opt into the revert — the contract is
visible, not implicit.

## Wrong-direction post-mortem (two rounds)

Two layers of wrong mental model surfaced during this
arc's drafting. Logging both because the second one
nearly slipped past:

### Round 1: the worktree-claim draft

A prior draft proposed moving `claim_task` and
`mark_pr_open` into per-task worktrees so the bd-state
changes would land on the task branch. The reviewer
flagged that this would break `turma status` and
reentrancy: a `turma status` run from main reads main's
bd state, but in the proposed model main's state would
lag (the claim hasn't been merged yet) until PR-merge.

That direction was retired.

### Round 2: the "shared via cwd-walkup" mental model
### (also wrong)

The retired direction's reasoning leaned on a NEW
claim: "`.beads/embeddeddolt/` is gitignored, so
worktrees don't get their own copy. bd walks up from
cwd to find the db, which means worktrees and main
share one dolt db." This claim made it into the
shipped spec as a load-bearing fact.

**Task 0 (2026-04-26) verified this empirically and
found it was also wrong.** Worktrees DO get their own
`.beads/` directory checked out from main's HEAD (the
config files, hooks, README — everything except
`embeddeddolt/`). bd from inside a worktree walks up,
finds the WORKTREE's `.beads/`, and lazily creates an
`embeddeddolt/` there on first invocation. Each
worktree has its OWN bd db. They are not shared.

If the prior draft had been written and shipped under
the shared-db assumption, `claim_task(cwd=worktree)`
would have mutated the WORKTREE's db, NOT main's.
`turma status` from main would still have read main's
db and seen the pre-claim state — the same status /
reentrancy bug the reviewer flagged would have
manifested, just for a different reason than the
post-mortem first claimed. The fix-sketch ("worktree
mutations affect the shared db, so status would have
been fine if not for the visibility lag") was the
wrong fix for the wrong reason.

### What's actually true (and what this arc relies on)

- Worktrees have their own `.beads/` directories with
  their own (separately-bootstrapped) dolt dbs.
- Turma invokes bd commands ONLY from main's working
  tree. Mutations land in main's bd db.
- Main's bd db is the canonical record of Turma's
  view of state. `turma status`, `bd ready`, `bd list`
  from main read it; status and reentrancy are
  preserved.
- Worker-side state propagation (worker's pre-commit
  hook in a worktree exporting bd state into the
  worker's commit, which then propagates via PR
  merge to origin/main) works through bd's own
  internal worktree-handling logic. v1 takes this as
  empirically verified (smoke iter-1 captured the
  right state) without modeling bd's internals.

This arc's actual fix only addresses the working-tree
dirtiness on main: `bd update` on main writes AND
stages `.beads/issues.jsonl`; Turma reverts via `git
restore --staged --worktree` after each mutation.
That's it. The dolt db never needed to move; the
mental-model correction matters only because it
changed the documentation, not the implementation
shape of `revert_paths`.

### Process learning

Two arcs in a row (`swarm-fetch-and-ff-base-
correction` was the first) were saved by Task-0-style
empirical verification. The pattern: a clean-looking
spec built on an unverified assumption about a
subsystem's behavior; live testing reveals the
assumption is wrong; the spec needs revision before
implementation.

Future arcs that touch git or bd should treat
"reach for the actual binary and run the operation
in a tmpdir" as a Task 0 prerequisite when the spec
makes a load-bearing claim about either system's
behavior.

This pattern — "wrong mental model produces wrong
direction; verifying the model produces a smaller
correct fix" — is the same pattern that produced
`swarm-fetch-and-ff-base-correction` (the colon-form
mental model was wrong, verifying real git produced a
correct three-call form). Logging it here so future
arcs treat unverified-mental-model as a leading cause
of overscoped corrections.

## Mock-test impact

`tests/test_swarm_run.py`:

- The happy-path test (`test_single_task_happy_loop`)
  asserts that `revert_paths` fires after `claim_task`
  and after `mark_pr_open`, with `.beads/issues.jsonl`
  in the path tuple.
- New ordering test:
  `test_revert_beads_export_runs_after_each_bd_mutation`
  pins all five callsite-pairs (mutation → revert) by
  inspecting StubGit + StubBeads call order.
- `StubGit` gains a `revert_paths` method recorded in
  its calls list. Existing assertions that check
  `git_steps == [...]` are extended to include the new
  `revert_paths` entries.
- The merge-advancement happy-path tests
  (`test_merge_advancement_merged_path` and
  `test_merge_advancement_closed_without_merge_*`)
  assert one revert call per dispatch arm.

`tests/test_swarm_git.py`:

- New tests for `revert_paths`:
  - argv-pin: `git -C <repo_root> checkout -- <paths>`.
  - empty paths: no subprocess call, returns None.
  - multi-path: paths passed verbatim in argv order.
  - failure surfaces stderr.

`tests/test_swarm_run.py` regression test for the
chained flow extends to confirm the existing
`test_chained_feature_post_merge_advances_dependent`
still passes with the revert calls inserted.

## Real-git integration test

A new case in `tests/test_swarm_git_integration.py`:

1. Bare remote + working clone with `main` checked out
   and `.beads/issues.jsonl` committed at version V0.
2. Locally modify `.beads/issues.jsonl` (simulating
   bd's post-update hook re-export).
3. Push a new commit to bare remote that ALSO modifies
   `.beads/issues.jsonl` (simulating a worker commit
   from a worktree, captured in a PR-merge).
4. Call `GitAdapter.revert_paths(working_clone,
   (".beads/issues.jsonl",))`.
5. Assert working tree is clean.
6. Call `GitAdapter.fetch_and_ff_base(working_clone,
   "main")`.
7. Assert main's HEAD now matches origin's tip and
   working tree is clean.

This pins the end-to-end model: the revert lets
`fetch_and_ff_base` succeed where the dirty version
refused.

## Failure modes

- `git restore --staged --worktree -- <paths>` non-zero
  exit: surfaces as `PlanningError("git restore failed:
  ...", stderr_preserved)`. Possible causes: file
  doesn't exist (operator deleted it), git config
  issues, or the file isn't tracked. Halts the run;
  operator triages.
- The revert is run on main's `repo_root`; if the
  orchestrator is somehow invoked from a non-git dir,
  it would have failed earlier in preflight. v1
  doesn't double-check.
- If `.beads/issues.jsonl` is gitignored on a
  particular operator's machine (path 1 done locally
  via `.git/info/exclude`), the revert is a no-op
  (file isn't tracked) and bd's export still fires
  but git doesn't see the change. The system still
  works.

## Shareability contract: what this arc actually changes

The reviewer flagged that this arc has a real
shareability consequence. Calling it out explicitly
because the prior draft glossed over it.

### Pre-existing propagation pathway (unchanged)

Today, Turma's bd-state mutations propagate to git via
**worker commits**, not via direct commits to main.
When the worker runs `git commit` inside its worktree,
bd's pre-commit hook fires and exports bd state into
the worktree's `.beads/issues.jsonl`. The worker's
commit captures that export. After PR merge,
origin/main has that snapshot.

The export captures "what bd would say is the current
state at hook-fire time". Crucially, this captured
state aligns with what Turma's main-cwd mutations
wrote — verified by the 2026-04-26 smoke iter-1, where
the worker's commit's issues.jsonl reflected the
post-`claim_task` `in_progress` status that
`claim_task` had just set from main's working tree.
Whatever bd does internally to keep these aligned
across the main↔worktree boundary, it works for v1's
purposes; this arc takes that as given.

The set of mutations captured by a given worker commit
is "everything bd's hook sees at the moment the worker
ran `git commit`". This includes mutations made earlier
in the same `turma run` iteration (e.g. `claim_task`
fired before the worker started) AND any mutations
from prior iterations that haven't yet been
propagated.

Mutations made AFTER a worker commit (e.g.
`mark_pr_open` fires after `open_pr` returns, which is
after `push_branch` and the worker's commit) live in the
local dolt db but haven't been captured by any commit.
They wait for the **next** worker commit to propagate.

### What this arc changes

Pre-fix, those un-captured tail mutations also dirtied
main's working tree. An operator running `git status`
could see them. They couldn't be propagated easily
(committing to main directly is a separate decision),
but they were at least visible.

Post-fix, the revert clears the working-tree visibility.
The tail mutations live in local dolt but no longer
appear in `git status`. The propagation pathway is
unchanged — the next worker commit still captures them
— but operators inspecting git state won't see anything.

### Concrete consequences

- **Same-operator turma run reentrancy**: works
  (reads from main's local dolt; sees latest state).
- **Same-operator turma status**: works (same).
- **Tail mutation persistence across one turma run**:
  unchanged. Still in local dolt; still not in git.
  If the local dolt is destroyed (rebuilt from
  `issues.jsonl`, machine restored from backup, etc.)
  before a future worker commit captures it, the
  tail mutation is lost.
- **Multi-clone propagation**: another operator who
  pulls origin/main sees state up to the last
  PR-merged worker commit's snapshot. Tail mutations
  from another clone's most recent `turma run`
  aren't visible until that clone's next worker
  commit propagates them. **This is a real lag for
  shared-bd-state deployments.**
- **Freshly rebuilt local dolt**: rebuilt from
  `issues.jsonl`, so it reflects only what's in
  origin/main. Local tail mutations would be lost.

### v1 acceptance and rationale

v1 explicitly accepts this contract for these reasons:

1. **It's a long-standing property of bd-in-Turma**,
   not introduced by this arc. The dirty-tree problem
   that motivated this arc was actually masking the
   underlying tail-propagation behavior. Pre-fix, the
   tail problem existed; nobody could observe it
   because iter-2 fetch refused and the chain
   stalled. The fix surfaces the property by making
   the system actually progress.
2. **Turma's primary v1 deployment target is
   single-operator**: one human running `turma run`
   on their checkout. Multi-clone state lag isn't
   load-bearing for that target.
3. **Adding "Turma commits + pushes bd-state changes
   to origin/main"** would require Turma to have
   direct push permission to the base branch. Most
   PR-review repos don't grant that. Doing it via PRs
   adds significant overhead (one synthetic PR per
   bd mutation, or batch PRs at end of iteration).
   Out of scope for this narrow stabilization arc.
4. **Operators concerned about multi-clone lag** can
   manually `bd export` after a turma run to refresh
   `issues.jsonl`, then commit and push. This is a
   documented escape hatch, not a routine step.

### Deferred: stricter shareability via explicit Turma commits

A future arc could add a `_commit_and_push_beads_state`
step at the end of each `turma run` iteration:

1. After all bd mutations for the iteration complete,
   `bd export` the dolt to `issues.jsonl`.
2. Commit the change on local main with a deterministic
   message (e.g. `bd: turma run @ <feature> @ <iter>`).
3. Push to origin/main (requires push permission) OR
   bundle into the next worker commit on the next
   iteration's task branch.

Direct push to main is operationally invasive and
requires repo-level permission grants. v1 doesn't
attempt it. A future arc that demonstrates a real need
(e.g. multi-operator chained-feature workflows) can
revisit.

A simpler "best effort" alternative: at end of each
`turma run`, the orchestrator prints a one-line warning
when there are tail mutations in local dolt that aren't
in origin/main yet, telling the operator to consider
running `bd export && git commit -- .beads/issues.jsonl`
manually. v1 could ship this telemetry without
committing to the full auto-commit model.

This arc deliberately ships without the warning to
keep scope narrow. If the smoke validation shows the
need, the warning can land as a small follow-up
without re-opening the spec.

## Migration notes

- **No code changes outside `git.py`, `_orchestrator.py`,
  and tests.** BeadsAdapter unchanged. Worktree code
  unchanged. The fix is entirely in the
  "after-bd-update cleanup" boundary.
- **Existing happy-path tests update once** to include
  the new `revert_paths` call in the expected git-call
  sequence.
- **The chained-flow regression test is extended**, not
  replaced. The orchestrator-contract scope (Findings 1
  + 2 + 3 from the prior arc) stays the same; the new
  revert behavior is layered onto the existing fixture.
- **Operator-visible behavior** is unchanged. PRs still
  have one worker commit per task. `bd list` /
  `turma status` show the same in-flight state. The
  only difference is `git status` no longer shows
  `.beads/issues.jsonl` as dirty after a `turma run`
  iteration completes.

## Open items

- **bd's worktree-handling internals**. Task 0
  empirically verified that worktrees have their own
  `.beads/` directories (NOT shared via cwd-walkup as
  the prior draft assumed) but that worker commits in
  worktrees still capture bd state correctly. The
  exact mechanism — whether bd uses the
  `.beads/.gitignore`-mentioned "worktree redirect
  file", whether the pre-commit hook reaches into
  main's git-dir, or something else — is bd-internal
  and not modeled by Turma. v1 treats it as opaque
  machinery that works. If a future change to bd
  breaks the propagation, this open item gets revisited.
- **bd `export.auto=false` as a future simplification**
  is rejected by Task 0: it suppresses the
  post-update jsonl export AND the pre-commit hook's
  export, breaking worker-commit propagation. The
  revert approach is the right shape; not a
  fallback-for-older-versions.
- **Sweep-phase mutations on closed tasks** stay on
  main with this approach (no relocation needed). The
  revert handles them. No follow-up arc required for
  iter-3+ chained flows.
- **Multi-operator concurrent claims** continue to
  rely on bd's optimistic concurrency model (push
  conflict surfaces as non-FF). Documented as
  expected v1 behavior; not affected by this arc.
