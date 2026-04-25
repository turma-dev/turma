## Why

The `swarm-post-merge-advancement` arc shipped the contract
that `turma run`'s success path stops at `mark_pr_open` and a
later run's merge-advancement sweep performs the close +
cleanup. The validation smoke against
`khanhgithead/turma-run-smoke` on 2026-04-25 walked a
two-task chained feature end-to-end and surfaced three real
issues that, taken together, mean a chained feature still
cannot make forward progress through a merge without operator
intervention.

The smoke output is preserved in
`/tmp/smoke-merge-run1.log` and `/tmp/smoke-merge-run2.log`;
the failure mode against the chained spec was reported in
the conversation that produced this change.

This is a narrow stabilization pass for that arc, not a new
feature. Its only user-visible promise: a chained feature
where task A blocks task B and PR for A has been merged on
GitHub will, on the next `turma run`, close A cleanly,
update the local base branch from origin, and run task B
against the merged base.

## Findings the smoke surfaced

1. **Reconciliation false-positives merged tasks as
   `completion-pending`.** When a `turma run`-opened PR has
   been merged between runs, the prior run's
   `mark_pr_open`-marked task is still `in_progress` with
   `.task_complete` on disk and a `turma-pr:<N>` label. The
   next run's reconciliation calls
   `find_open_pr_url_for_branch`, which queries
   `--state open` only. The merged PR is no longer in that
   list, so the task is classified as `completion-pending`,
   the repair phase commits + pushes (no-op) + re-opens a
   duplicate PR against the same branch. The
   merge-advancement sweep then runs and closes the task
   off the original `turma-pr:<N>` label, leaving the
   duplicate PR open as noise.

2. **Stale `turma-pr:<N>` labels accumulate on closed
   tasks.** `BeadsAdapter.mark_pr_open` adds a label
   without removing prior `turma-pr:<M>` labels.
   `_extract_pr_number` picks the first valid label, so the
   sweep dispatches on the original number.
   `unmark_pr_open` removes only that specific N. When
   Finding 1 fires, the duplicate PR's label is added on
   top of the original, and after the sweep closes the
   task, the duplicate's label remains on a closed bd task.
   This is the case the prior arc explicitly deferred
   (`design.md` "Deferred: stale `turma-pr` label on a
   closed bd task") — Finding 1 makes it routine, not a
   corner case.

3. **Orchestrator does not refresh `base_branch` from
   origin before claiming a dependent task.** When the
   merge-advancement sweep closes the parent task,
   `bd ready` then surfaces the dependent. The main loop
   claims it and `WorktreeManager.setup` runs
   `git worktree add -b <branch> <path> <base_branch>`,
   which uses the LOCAL ref. Local `base_branch` was never
   updated to reflect the merge that happened on origin, so
   the dependent's worktree has the pre-merge tree. The
   worker correctly diagnoses the missing precondition and
   refuses (defensive worker behavior the new contract now
   relies on); the chain stalls.

Findings 1 and 2 are one bug family — the merge-tracked
state crossed two contracts (`reconcile` + `mark_pr_open`)
that were authored in separate arcs. Finding 3 is the
missing fetch step that chained features specifically
require. All three are within the same workflow boundary
(post-merge advancement of a chained feature) and are best
fixed together.

## What Changes

- **Reconciliation becomes label-aware.** Tasks carrying a
  valid `turma-pr:<N>` label are skipped during reconciliation
  classification (no finding emitted, no repair-phase action).
  Merge-advancement remains the sole owner of merge-tracked
  task transitions. Reconciliation logs the skip with a single
  line per task so operators can see the routing decision.
- **`mark_pr_open` becomes set-of-one.** Before adding
  `turma-pr:<N>`, the adapter removes any other
  `turma-pr:<M>` (M ≠ N) labels on the task. Combined with
  the existing idempotency precheck, the adapter guarantees
  that a task has at most one `turma-pr:` label at any time.
  The precheck-skip path (label already exactly N) is
  unchanged.
- **`GitAdapter` gains a `fetch_and_ff_base(base_branch)`
  method.** Single-call argv pinned:
  `git -C <repo_root> fetch origin
  <base_branch>:<base_branch>`. The colon-form fast-forwards
  the local ref without disturbing the operator's HEAD or
  current branch and refuses divergent local with a non-zero
  exit. Subprocess failures (network, auth, divergent local)
  surface as `PlanningError` with bd-style stderr-preserving
  messages; divergence gets a typed message that names the
  branch and points the operator at
  `git log <branch>..origin/<branch>` for triage. See
  `design.md` "`GitAdapter.fetch_and_ff_base`" for the
  rationale on choosing the single colon-form over a
  separate `fetch + merge --ff-only` pair.
- **`run_swarm` calls `fetch_and_ff_base` once per
  invocation**, after preflight and before reconciliation.
  Skipped under `--dry-run` (the fast-forward mutates a local
  ref). The orchestrator now starts every run with a
  base_branch that matches origin.
- **Docs + CHANGELOG**: README Swarm Execution and
  `docs/architecture.md` Execution diagrams gain the new
  base-branch-sync step at the top of the state machine.
  `docs/smoke-turma-run.md` Step 3's expected log gains the
  fetch line; the chained-task narrative is added explicitly
  so the runbook captures the dependency-unblock path the
  Apr 25 smoke surfaced.
- **Validation**: full pytest green plus a re-walk of the
  same two-task chained smoke against
  `khanhgithead/turma-run-smoke` to confirm task 2 claims
  cleanly against the merged base.

## What does NOT change

- **Merge-advancement dispatch contract is unchanged.** The
  three states (`OPEN` / `MERGED` / `CLOSED`) and the
  per-state actions stay as they shipped. This arc only
  fixes the *inputs* to the sweep (which tasks are
  merge-tracked, with how many labels) and the *base ref*
  the main loop uses.
- **Reconciliation's six finding types are unchanged.** No
  new `MergeTracked` finding is added. The decision to skip
  rather than emit a new finding keeps the
  `ReconciliationReport` shape stable. Operators see
  merge-tracked tasks via `turma status`'s in-progress
  section (which already renders `pr: #N (state) url` from
  the prior arc) and via the reconciliation skip log line.
- **The deferred orphan-branch decision (Option A spec
  change vs Option B log fix) stays parked.** It is
  orthogonal to the chained-feature failure mode this arc
  fixes.
