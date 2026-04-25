## Why

`turma run` currently opens a PR per task and closes the Beads
task in the same step (`open_pr` → `close_task` → `cleanup_worktree`,
all inside `_run_single_task`). bd's dependency graph then
considers the task done, and any dependent tasks become ready
the moment `turma run` opens the parent's PR — long before a
human has reviewed or merged it.

That has three concrete operator costs today:

1. **Dependents claim against an unmerged base.** When task A
   blocks task B and `turma run` opens A's PR, bd marks A
   closed and B becomes ready. The very next iteration claims
   B, creates a worktree from `main`, and invokes the worker
   against a tree that does NOT yet contain A's commits. The
   smoke runbook side-steps this by running `--max-tasks 1`,
   but a real feature with chained dependencies cannot use the
   sequential loop without intervention.

2. **PR-merge state is invisible to the orchestrator.** Once
   the PR opens, turma's record of the world ends. If the
   reviewer merges the PR, nothing in turma observes that. If
   the reviewer closes the PR without merging, nothing in
   turma observes that either — the operator has to manually
   re-open / re-attempt the task.

3. **Reconciliation already has the bones for this.** The
   read-only reconciliation module classifies prior-run state
   into typed findings before the main loop starts. A "scan
   PRs and advance Beads accordingly" sweep at the same point
   in the run is a natural extension, not a new architectural
   layer.

This change adds **post-merge advancement**: a single sweep at
the top of `turma run` (after preflight + reconciliation +
repair, before `fetch_ready`) that observes GitHub PR state for
the feature's outstanding task PRs and advances Beads
accordingly. The success path of `_run_single_task` stops at
`open_pr` — the Beads close + worktree cleanup move to the
sweep, which fires only after GitHub reports the PR as merged.

Deliberately v1 scope: single-feature, sequential, one sweep
per `turma run` invocation, no background polling, no global
scheduler, no parallel claims. Same scope discipline as
`turma-status`.

## What Changes

- **Success path of `_run_single_task` stops at `open_pr`.**
  Instead of `close_task` + `cleanup_worktree`, the orchestrator
  records the PR number on the Beads task via a new
  `turma-pr:<N>` label and leaves the task in `in_progress`
  with its worktree on disk. The branch + PR + worktree triple
  is the "work submitted, awaiting merge" state.
- **New phase between `_apply_repairs` and `_main_loop`.**
  `_advance_merged_prs(feature, services)` queries every
  `in_progress` task that carries a `turma-pr:<N>` label and
  dispatches on the PR's GitHub state:
  - `MERGED` → remove the label, `close_task`,
    `cleanup_worktree`.
  - `OPEN` → leave alone (merge hasn't happened yet). Draft
    PRs return `state == OPEN` from `gh`'s `--json state`
    output (draftness lives on a separate `isDraft` boolean
    field that v1 does not query); they fall through this
    branch and are treated identically.
  - `CLOSED` (without merge) → `fail_task` with reason
    `"PR #<N> closed without merge"` so the retry budget
    applies; worktree stays on disk for triage per the v1
    Worktree contract.
- **Repair phase mirrors the new model.**
  `_complete_pending_task` (the `completion-pending` repair)
  ends at the PR-open + label step, not at `close_task` +
  cleanup. `completion-pending-with-pr` becomes a label-add +
  leave-alone (the merge-advancement phase will close it on a
  future run if/when the PR merges).
- **Two small adapter additions, both narrowly scoped:**
  - `BeadsAdapter.mark_pr_open(task_id, pr_number)` and
    `unmark_pr_open(task_id, pr_number)` — wrap the existing
    `bd update --add-label` / `--remove-label` argv used by
    `fail_task` so the orchestrator doesn't reach into raw
    label strings.
  - `PullRequestAdapter.get_pr_state_by_number(pr_number)` —
    `gh pr view <N> --json number,state,url` returning the
    PR's current state (`OPEN` / `MERGED` / `CLOSED`; v1
    does not differentiate drafts, which return `OPEN`
    here). `list_prs_for_feature` from the turma-status arc
    covers the feature-scoped batch case but indexes by
    branch; the advancement sweep needs to look up by the
    recorded PR number directly.
- **`README.md` and `docs/architecture.md`** Execution sections
  updated for the new state machine. The smoke runbook gets a
  new step illustrating the merge-advancement path.
- **`turma status`** counter labels stay the same — a task
  awaiting merge is `in_progress` with a `turma-pr:<N>` label.
  The status readout's in-progress section will surface the PR
  link and merge state inline (small additive change).

## Capabilities

### New Capabilities

- `merge-advancement`: read-only sweep at the top of `turma
  run` that observes GitHub PR state and advances Beads to
  match. Mirrors reconciliation's discipline — detection
  separated from mutation, single pass per invocation.

### Modified Capabilities

- `swarm-orchestration` success path: the open_pr-then-stop
  shape replaces open_pr-then-close.
- `beads-adapter` gains a typed PR-label helper pair
  (`mark_pr_open` / `unmark_pr_open`) on top of the existing
  generic label mechanism in `fail_task`.
- `pull-request-adapter` gains a number-indexed state query.
- `run-reconciliation` repair phase: `completion-pending` and
  `completion-pending-with-pr` repair actions update to the
  new label-and-defer model.

## Impact

- **New files:** none. The `_advance_merged_prs` phase lives
  in `src/turma/swarm/_orchestrator.py` alongside
  `_apply_repairs`.
- **Modified files:**
  - `src/turma/swarm/_orchestrator.py` — new phase + success-
    path shape change + repair-action updates.
  - `src/turma/transcription/beads.py` — `mark_pr_open` /
    `unmark_pr_open` helpers.
  - `src/turma/swarm/pull_request.py` —
    `get_pr_state_by_number`.
  - `src/turma/swarm/status.py` — surface the recorded PR
    number + merge state in the in-progress section (small
    additive).
  - `tests/test_swarm_run.py` — happy-path tests update from
    "expects close_task" to "expects mark_pr_open"; new tests
    for the merge-advancement phase across MERGED / OPEN /
    CLOSED-unmerged.
  - `tests/test_swarm_reconciliation.py` — repair-action
    expectations updated.
  - `tests/test_transcription_beads.py` — argv pinning for the
    label helpers.
  - `tests/test_swarm_pull_request.py` — argv + parsing for
    `get_pr_state_by_number`.
  - `tests/test_swarm_status.py` — in-progress section
    rendering picks up the PR-state addition.
  - `README.md` Swarm Execution section.
  - `docs/architecture.md` Execution state machine.
  - `docs/smoke-turma-run.md` — new step demonstrating the
    merge-advancement path against a real `gh pr merge`.
  - `CHANGELOG.md` `[Unreleased]`.
- **No new runtime deps.** `bd` and `gh` are already
  prerequisites.

## Out of Scope

- Parallel task execution. Each `turma run` invocation still
  processes one ready task at a time before fetching the
  next batch.
- Background polling / watch mode. `turma run` is the trigger;
  if the operator wants advancement to fire after a merge,
  they re-run `turma run`.
- Cross-feature advancement. The sweep is feature-scoped, just
  like the rest of `turma run`.
- Multi-PR-per-task scenarios. The orchestrator opens exactly
  one PR per task; if the operator manually opens additional
  PRs against a task branch, only the recorded `turma-pr:<N>`
  is queried.
- Merge-conflict handling. If `gh pr view` reports the PR is
  in a state that prevents merge (e.g. mergeable=CONFLICTING),
  v1 surfaces the state but does not attempt to resolve.
- Reverting a merged PR. If a PR was merged then reverted on
  GitHub, the Beads task is already closed; this change set
  doesn't reopen it. Operator triage.
- Squash / rebase / merge-commit strategy preference. The
  merge-advancement phase only reads GitHub state; the merge
  itself is the operator's choice.
