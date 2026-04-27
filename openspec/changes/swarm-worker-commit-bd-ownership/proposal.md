## Why

The 2026-04-26b chained-flow smoke against
`khanhgithead/turma-run-smoke-2` (run after the
`swarm-beads-state-merge-cleanliness` arc shipped on main)
surfaced two further bd-state defects on top of the already-fixed
revert-after-mutation flow:

1. **Finding 1 — read-side dirtying.** `bd list --json` (and
   likely other bd reads) re-dirties `.beads/issues.jsonl` between
   `turma run` invocations. With the just-shipped preflight in
   place, the next iteration refuses to start. Cause: bd's
   `export.interval` (default 60s) defers exports; the **next** bd
   command — read or write — fires the deferred export.

2. **Finding 2 — wrong-path worker commit.** The worker's
   commit captures `D .beads/issues.jsonl, A issues.jsonl (at
   repo root), A <task-file>` instead of the expected `A
   <task-file>` plus a clean `.beads/issues.jsonl` update. Once
   that PR merges, main's HEAD has `.beads/issues.jsonl` deleted
   and a stray `issues.jsonl` at the project root. The next
   iteration's `revert_paths` then fails because the path is no
   longer tracked, and the orchestrator halts.

Both are release-blocking for 0.3.0. Both were silently present
in earlier smoke runs (the prior smoke repo's PRs #5 and #7 show
the exact same wrong-path diff shape) — the new arc's regression
checks just made them visible.

## Key insight

A no-agent shell-only reproducer locks Finding 2 to bd, NOT
Turma or Claude Code:

```
git worktree add -b probe .worktrees/probe <base-with-issues.jsonl>
cd .worktrees/probe
bd prime                                    # ⚠️ silently deletes .beads/issues.jsonl
echo "..." > STAGE.txt
git add -A
git commit -m "..."                          # bd pre-commit hook misroutes export
```

The instrumented Claude Code probe (`/tmp/probe-stream.json`)
showed the agent makes only `Write <task-file>` and `Write
.task_complete` tool calls. No `Bash`, no `bd export`, no
`git mv`. The wrong-path artifact comes entirely from bd's
pre-commit hook firing against an index that already has
`D .beads/issues.jsonl` staged (the deletion that `bd prime`
itself caused). bd's hook stdout claims it exported to
`.beads/issues.jsonl`, but the staged path lands at the repo
root.

This is **upstream bd's defect**, drafted as a separate
filing in the planning repo at
`docs/upstream-bd-worktree-precommit-bug.md`.

Finding 1's mechanism is unrelated: bd's
`export.interval=60s` throttle defers writes; the next bd
command (read or write) flushes the pending export, dirtying
the file.

## Constraints preserved

- The existing revert-after-mutation contract from
  `swarm-beads-state-merge-cleanliness` is correct and stays.
- The dirty-bd-state preflight is correct and stays.
- The tail-mutation warning at end-of-`run_swarm` stays.
- Worker-commit propagation (the pathway by which bd state
  reaches origin/main when a worker PR merges) must keep
  working — this is the contract Task 0 of the prior arc
  ruled out `export.auto=false` for breaking.

## Outcome

Turma stops generating the wrong-shaped commit instead of
papering over it after the fact. Two layered changes:

1. **`export.interval=0` Turma contract.** Operator sets it
   in `bd config set export.interval 0`; Turma's preflight
   verifies and refuses otherwise. Solves Finding 1.
   Verified empirically: with `export.interval=0`, bd writes
   export immediately at write time (so the existing revert
   can clean them) and reads NEVER trigger an export. Worker-
   commit propagation is unaffected (writes still export).

2. **Turma owns bd export at the worker-commit boundary.**
   In `commit_all` (the worker's commit step), Turma runs an
   explicit `bd export -o <worktree>/.beads/issues.jsonl`
   from main's repo root (where Turma's bd db with the
   claim-side mutations lives) BEFORE `git add -A`, then
   commits with `core.hooksPath=/dev/null` so bd's broken
   pre-commit hook never fires for this one commit. The
   worker-commit propagation pathway is preserved by the
   explicit export — same data, same path, just produced by
   Turma directly instead of relying on bd's misbehaving
   hook.

## Out of scope

- **The bd upstream issue** is drafted separately in the
  planning repo. Filing it is a public/visible action that
  needs explicit operator confirmation and is not bundled
  with this PR.
- **Other bd hook bypasses.** Only `commit_all` (the
  worker's per-task commit) is affected; bd's other hooks
  (post-checkout, post-merge, prepare-commit-msg) are not in
  scope. Turma never invokes commits in main's working tree
  from inside a `turma run` flow today.
- **A defensive `revert_paths` for missing-path** (Finding
  3 in the smoke triage). Stops being needed once Finding
  2 is fixed. If upstream bd ships a fix that re-routes the
  export back to `.beads/issues.jsonl` correctly, this
  arc's hook bypass becomes redundant but stays harmless.
