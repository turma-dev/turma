## Why

The 2026-04-26 chained-flow live smoke against
`khanhgithead/turma-run-smoke`, exercising the just-shipped
three-call `fetch_and_ff_base` from
`swarm-fetch-and-ff-base-correction`, failed at iteration 2:

```
error: git merge --ff-only failed: exit 1
error: Your local changes to the following files would be
overwritten by merge:
  .beads/issues.jsonl
Please commit your changes or stash them before you merge.
Aborting
```

Root cause: bd's tracked state file `.beads/issues.jsonl`
got dirtied between iterations by orchestrator-driven bd
mutations (`claim_task`, `mark_pr_open`) running against
**main's working tree**. Iteration 2's `merge --ff-only`
correctly refused. The fix from
`swarm-fetch-and-ff-base-correction` is working as
designed; this finding is at a different layer.

This is release-blocking for 0.3.0.

## Key insight

bd's own `.beads/.gitignore` confirms the design split:

```
# Dolt database (managed by Dolt, not git)
dolt/
embeddeddolt/
...
```

`.beads/embeddeddolt/` (the dolt database that holds bd's
actual state) is **local-only**, gitignored. The
**only** tracked bd-state file is `.beads/issues.jsonl`,
which is a text export written by bd's hooks on every
`bd update`.

bd looks for `.beads/` by walking up from cwd. Worktrees
don't get their own `embeddeddolt/` (it's gitignored, so
nothing's checked out there); a `bd` invocation from
inside a worktree walks up and finds the main repo's
`.beads/embeddeddolt/`. **There is exactly one dolt db per
repository, shared across the main checkout and all
worktrees.** Mutations from any cwd land in that one db.

This means:

- `bd ready`, `bd list`, `bd show` always read from the
  one shared dolt db. They reflect the latest mutation
  regardless of what `.beads/issues.jsonl` looks like.
- `turma status` reads via `bd list` → reads the dolt db
  → sees in-flight state immediately after `claim_task`.
- A second `turma run` invocation reads `bd ready` →
  same dolt db → won't re-claim an already-claimed task.
- `.beads/issues.jsonl` is a derived export. Its
  working-tree state is regenerable; the dolt db is the
  source of truth.

## Why "commit at point of creation" was the wrong direction

A prior draft of this spec proposed moving `claim_task`
and `mark_pr_open` into per-task worktrees so their
mutations would land on the task branch. The reviewer
caught that this trades the dirty-tree bug for two worse
bugs:

- `turma status` would read from main's checkout, but in
  the proposed model main's bd state would lag (the
  claim hasn't been merged yet) — status would lie about
  in-flight tasks.
- A second `turma run` invocation before the PR merges
  would read main's bd state, see the task as still
  ready, and re-claim it.

Both regressions stem from assuming the worktree's bd db
is separate from main's. With the dolt-as-walked-up-from-
cwd model verified above, that assumption is **false**:
all bd mutations land in the same shared db regardless
of cwd. The previous draft's "fold bd state into the
task branch" reasoning was based on a wrong mental
model.

The actual problem is narrower: only the working-tree
state of `.beads/issues.jsonl` (the derived export) is
dirty. The dolt db has the right state and never needed
to move.

## What Changes

- **Preflight refuses to start if `.beads/issues.jsonl`
  is already dirty in main's working tree.** Turma's
  ownership of that file's working-tree state holds only
  if Turma starts from a clean baseline; if the operator
  has pre-existing uncommitted changes (from manual `bd
  update` runs, a crashed prior `turma run`, or hand
  edits), Turma must NOT silently revert them. Instead,
  the orchestrator halts with a typed `PlanningError`
  naming the file and giving the operator three triage
  commands (`git diff`, `git stash push --`,
  `git checkout --`). Skipped under `--dry-run` (which
  doesn't mutate bd state).
- **The orchestrator owns the working-tree state of
  `.beads/issues.jsonl` on main.** After each Turma-
  initiated `bd update` mutation in the main checkout
  (claim, mark_pr_open, unmark_pr_open, close_task,
  fail_task), the orchestrator runs
  `git -C <repo_root> checkout -- .beads/issues.jsonl`
  to restore the file to its index version. The dolt db
  retains the mutation; the export gets discarded;
  working tree stays clean. The preflight check above
  guarantees this revert only ever discards Turma's own
  hook export, never operator changes.
- **A new `GitAdapter.revert_paths(repo_root, paths)`
  helper** wraps the checkout call. Argv pinned:
  `git -C <repo_root> checkout -- <path1> <path2> ...`.
  Failure surfaces as `PlanningError` with stderr
  preserved. Empty paths is a no-op (no subprocess
  call).
- **`BeadsAdapter` mutation methods are unchanged** —
  no `cwd` parameter, no relocation of where they run.
  The fix is at the orchestrator layer, not the adapter.
- **Five callsite changes in `_orchestrator.py`**: a
  `_revert_beads_export(services)` helper called after
  each bd-mutation point. The points:
  1. `claim_task` (top of `_run_single_task`)
  2. `mark_pr_open` (success-path tail of
     `_run_single_task`, and the
     `CompletionPendingWithPr` repair arm)
  3. `unmark_pr_open` (merge_advancement_phase MERGED
     and CLOSED dispatches)
  4. `close_task` (merge_advancement_phase MERGED
     dispatch)
  5. `fail_task` (merge_advancement_phase CLOSED
     dispatch and main_loop failure path)
- **Real-git integration test**: a new case in
  `tests/test_swarm_git_integration.py` that mutates a
  bd-tracked file in a working clone, reverts it via
  `GitAdapter.revert_paths`, then exercises
  `fetch_and_ff_base` against a remote that updated the
  same file. Pins clean-tree-across-iterations against
  real git behavior, parallel to the colon-form-bug
  regression test.
- **Subprocess-mock tests** in `tests/test_swarm_run.py`
  assert that each bd-mutation point is followed by a
  `revert_paths` call with `.beads/issues.jsonl` in the
  path list. Existing happy-path tests update once to
  include the new revert calls in the expected git-call
  sequence.
- **`docs/architecture.md`** Execution section gains a
  short "bd-state ownership" subsection naming Turma as
  the owner of `.beads/issues.jsonl`'s working-tree
  state in the main checkout, with the dolt db as
  source of truth.
- **`docs/smoke-turma-run.md` Step 3a** gains two
  `git status --short` regression checks (one after
  iteration 1, one after iteration 2). These ARE the
  regression contract for this arc; the runbook is the
  source of truth for reproducing the smoke from docs
  alone, so the contract has to live there, not in
  chat. Each check is paired with an explanatory
  sentence so the operator knows what dirty output
  would mean (a missing `_revert_beads_export`
  callsite).
- **`CHANGELOG.md` `[Unreleased]/Fixed`** amended with
  the iter-2 finding + fix.
- **No README changes.** The "Base-branch sync"
  subsection is unaffected.
- **Live smoke re-run** against
  `khanhgithead/turma-run-smoke` walks Step 3a end-to-end
  one more time. Closes the manual-smoke `[ ]` on
  `swarm-fetch-and-ff-base-correction` (Task 4) and
  `swarm-merge-advancement-stabilization` (Task 7) since
  both were waiting on this arc to land.

## Why this is not a path-3 workaround

The reviewer flagged the prior draft's auto-stash idea
as "a Git-symptom workaround that ages badly." The
ownership distinction matters here:

- **Path 3 (rejected)**: `fetch_and_ff_base` stashes any
  dirty file before merge, restores after. Untargeted,
  brittle, hides ownership behind a low-level adapter.
- **This proposal**: the orchestrator reverts a single
  named file after each known mutation. Targeted,
  deterministic, and ownership lives where the mutation
  was initiated. The dolt db is the source of truth;
  reverting the export is sound by bd's design.

The `revert_paths` helper has no special knowledge of
`.beads/issues.jsonl` — callers pass the path explicitly.
That keeps the helper general while concentrating the
"Turma owns this file" decision at the orchestrator
callsites.

## Shareability contract (explicit acceptance)

This arc DOES affect bd's shareability semantics, in a
way the prior draft glossed over. Calling it out
explicitly because it is a real change:

- **The propagation pathway for Turma's bd-state
  mutations is unchanged.** Mutations on main's
  working tree (which become invisible after the
  revert) still reach git only via the **next worker
  commit**, whose pre-commit hook exports the shared
  dolt db state into the worktree's `.beads/issues.
  jsonl`.
- **The visibility of un-propagated tail mutations
  changes.** Pre-fix, tail mutations (the last
  `mark_pr_open` of a `turma run`, sweep-phase
  closes when no more workers run) showed up as a
  dirty `git status`. Post-fix, the revert clears
  them from the working tree. They live in local
  dolt only until a future worker commit captures
  them.
- **Multi-clone deployments will see a lag**: another
  operator pulling origin/main only sees bd state
  through the last PR-merged worker commit. Tail
  mutations from someone else's recent `turma run`
  aren't visible until that clone's next worker
  commit propagates them.

v1 explicitly accepts this contract. The reasoning,
v1 deployment-target framing, and a deferred
stricter-shareability follow-up are documented in
`design.md` "Shareability contract: what this arc
actually changes". TL;DR: Turma's primary v1 target
is single-operator; multi-clone shareability is a
follow-up if needed; auto-commit-and-push-bd-state
to main requires push permission most repos don't
grant.

## What does NOT change

- **`fetch_and_ff_base` contract** stays as
  `swarm-fetch-and-ff-base-correction` shipped (three-
  call symbolic-ref + fetch + merge --ff-only with the
  precheck).
- **`BeadsAdapter` mutation methods** stay as they are.
  No `cwd` param. No new methods.
- **`turma status` and `bd ready` reads** continue to
  work because they were always reading from the dolt
  db, not from `issues.jsonl`'s working-tree state.
  No status / reentrancy regression for the
  same-operator case.
- **The merge-advancement dispatch contract**, the
  set-of-one `mark_pr_open` invariant, the
  reconciliation skip for merge-tracked tasks, and the
  three-call `fetch_and_ff_base` precheck all stay as
  prior arcs shipped them.
- **Operator-facing PR review experience** is unchanged.
  No extra commits on task branches. PRs still have
  the worker's single commit (which already includes
  the export from the worktree's pre-commit hook firing
  on the worker's `git commit`).
- **The propagation pathway** for bd-state mutations
  (worker-commit-via-pre-commit-hook export). What
  changes is visibility, not pathway — see
  "Shareability contract" above.
- **README "Base-branch sync" subsection** — operator-
  facing prose unaffected.
