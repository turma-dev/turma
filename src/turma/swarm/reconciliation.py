"""Read-only reconciliation between Beads state and worktree filesystem.

At `turma run --feature <name>` start, the orchestrator walks the
authority model (Beads → git → sentinels) before the main loop so a
prior run's interrupted state is classified explicitly rather than
silently re-entered.

This module does the classification half of that walk. It queries
in-progress Beads tasks and inspects their worktrees, then returns a
typed `ReconciliationReport` the orchestrator's repair phase (Task 7
of `openspec/changes/swarm-orchestration/tasks.md`) consumes.

**Invariant: reconciliation never mutates.** It does not call
`fail_task`, `close_task`, `claim_task`, `git commit`, `git push`, or
`gh pr create`. The repair phase owns every state change; splitting
detection from mutation keeps this module trivially testable with
pure fixtures and gives the operator a single place (the repair
phase) to read what the orchestrator is about to change.

### Finding types

Six finding dataclasses, all emitted by `reconcile_feature`:

- `MissingWorktree`
- `CompletionPending`
- `CompletionPendingWithPr`
- `FailurePending`
- `StaleNoSentinels`
- `OrphanBranch`

`completion-pending-with-pr` disambiguation requires querying GitHub
for an open PR whose head is the task branch, so `reconcile_feature`
takes a `pr_adapter` in addition to the Beads / worktree / git
dependencies. The PR lookup is a read-only `gh pr list` call; no
mutation adapter surface is ever invoked.

### Merge-tracked tasks are NOT classified

In_progress tasks carrying a valid `turma-pr:<N>` label are owned by
the merge-advancement sweep, not by reconciliation / repair. Per
`openspec/changes/swarm-merge-advancement-stabilization/design.md`
"Reconciliation: skip vs new finding" (Option A), reconciliation
detects these tasks via `_extract_pr_number` and **skips them**
before any classification fires:

- The task is not added to `findings` — no finding type is emitted.
- The task is added to `merge_tracked` (an informational field on
  the report) so the orchestrator can log the routing decision.
- The task's branch IS still added to `claimed_branches`, so the
  orphan-branch loop does not misclassify it as orphan.

The "every in-progress task gets a finding" property the original
turma-status arc relied on is therefore retired: a labelled task
exists but has no finding. Operators see merge-tracked state via
the `reconcile: <id> → skipped (merge-tracked at PR #<N>)` log
line and via `turma status`'s in-progress `pr:` line.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from turma.transcription.beads import BeadsTaskRef, _extract_pr_number


# ---------------------------------------------------------------------
# Protocols — the slice of each adapter reconciliation depends on.
# Kept as Protocols so tests can inject minimal stubs without having
# to satisfy the full adapter surface.
# ---------------------------------------------------------------------


class _BeadsView(Protocol):
    def list_in_progress_tasks(
        self, feature: str
    ) -> tuple[BeadsTaskRef, ...]: ...


class _WorktreeView(Protocol):
    def worktree_path_for(self, feature: str, task_id: str) -> Path: ...
    def branch_name_for(self, feature: str, task_id: str) -> str: ...
    def list_task_branches(self, feature: str) -> tuple[str, ...]: ...


class _GitView(Protocol):
    """Reserved for future reconciliation checks (e.g. dirty-tree
    classification). The current implementation does not call it,
    but it is part of the signature per the design doc."""


class _PullRequestView(Protocol):
    def find_open_pr_url_for_branch(self, branch: str) -> str | None: ...


# ---------------------------------------------------------------------
# Finding dataclasses
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class MissingWorktree:
    task_id: str
    suggested_repair: str = (
        "release the claim (in_progress → open) so the task can be re-attempted"
    )


@dataclass(frozen=True)
class CompletionPending:
    task_id: str
    suggested_repair: str = (
        "run the normal commit/push/open-pr tail and close the Beads task"
    )


@dataclass(frozen=True)
class CompletionPendingWithPr:
    task_id: str
    pr_url: str
    suggested_repair: str = (
        "close the Beads task and remove the worktree"
    )


@dataclass(frozen=True)
class FailurePending:
    task_id: str
    reason: str
    suggested_repair: str = (
        "pass to fail_task with the worker's reason; leave the "
        "worktree on disk for triage"
    )


@dataclass(frozen=True)
class StaleNoSentinels:
    task_id: str
    suggested_repair: str = (
        "halt the run; operator inspects the worktree and decides"
    )


@dataclass(frozen=True)
class OrphanBranch:
    branch: str
    suggested_repair: str = "surface only; operator triage"


Finding = (
    MissingWorktree
    | CompletionPending
    | CompletionPendingWithPr
    | FailurePending
    | StaleNoSentinels
    | OrphanBranch
)


@dataclass(frozen=True)
class ReconciliationReport:
    """Ordered set of findings from a single reconciliation pass.

    `merge_tracked` is an informational field carrying the
    (task_id, pr_number) pairs reconciliation skipped because the
    task is owned by the merge-advancement sweep. Defaults to ()
    so existing tests / call sites that construct a
    ReconciliationReport without this field continue to work.
    No repair-phase action is associated with this list — it
    exists purely so the orchestrator can log the routing decision
    and so tests can assert on the skip set.
    """

    findings: tuple[Finding, ...]
    merge_tracked: tuple[tuple[str, int], ...] = ()


# ---------------------------------------------------------------------
# Sentinel file names — mirror the worker backend.
# ---------------------------------------------------------------------


_TASK_COMPLETE = ".task_complete"
_TASK_FAILED = ".task_failed"


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------


def reconcile_feature(
    feature: str,
    *,
    adapter: _BeadsView,
    worktree_manager: _WorktreeView,
    git_adapter: _GitView,
    pr_adapter: _PullRequestView,
    repo_root: Path,
) -> ReconciliationReport:
    """Classify prior-run state for `feature` and return the report.

    Never calls mutation methods on any adapter. `git_adapter` and
    `repo_root` are accepted for the stable signature the orchestrator
    wires in Task 7; v1 reconciliation does not consume them, but
    they stay so the interface is stable when future classification
    logic (e.g. dirty-tree-based disambiguation) lands.
    `pr_adapter.find_open_pr_url_for_branch` is called only for tasks
    carrying a `.task_complete` sentinel — never for fail / stale /
    missing-worktree tasks — so the cost is bounded by the number of
    completed-but-not-closed tasks, typically 0-1 after an interrupt.
    """
    del git_adapter, repo_root  # reserved for future classification

    in_progress = adapter.list_in_progress_tasks(feature)
    findings: list[Finding] = []
    merge_tracked: list[tuple[str, int]] = []

    claimed_branches: set[str] = set()
    for task in in_progress:
        branch = worktree_manager.branch_name_for(feature, task.id)
        claimed_branches.add(branch)
        # Merge-tracked tasks are owned by merge-advancement, not
        # reconciliation / repair. Skip classification entirely;
        # the branch still counts as claimed so orphan-branch
        # detection below excludes it.
        pr_number = _extract_pr_number(task.labels)
        if pr_number is not None:
            merge_tracked.append((task.id, pr_number))
            continue
        findings.append(
            _classify_task(feature, task, worktree_manager, pr_adapter, branch)
        )

    # Orphan branches: local task/<feature>/* branches with no
    # corresponding in_progress Beads task.
    branches = worktree_manager.list_task_branches(feature)
    for branch in branches:
        if branch in claimed_branches:
            continue
        findings.append(OrphanBranch(branch=branch))

    report = ReconciliationReport(
        findings=tuple(findings),
        merge_tracked=tuple(merge_tracked),
    )
    _print_summary(report, len(in_progress))
    return report


# ---------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------


def _classify_task(
    feature: str,
    task: BeadsTaskRef,
    worktree_manager: _WorktreeView,
    pr_adapter: _PullRequestView,
    branch: str,
) -> Finding:
    worktree = worktree_manager.worktree_path_for(feature, task.id)
    if not worktree.exists():
        return MissingWorktree(task_id=task.id)

    complete = worktree / _TASK_COMPLETE
    failed = worktree / _TASK_FAILED

    if complete.exists():
        # completion wins over failure if both exist — mirrors
        # ClaudeCodeWorker's _detect_sentinel_result precedence.
        pr_url = pr_adapter.find_open_pr_url_for_branch(branch)
        if pr_url:
            return CompletionPendingWithPr(task_id=task.id, pr_url=pr_url)
        return CompletionPending(task_id=task.id)
    if failed.exists():
        return FailurePending(
            task_id=task.id, reason=_read_reason(failed)
        )
    return StaleNoSentinels(task_id=task.id)


def _read_reason(path: Path) -> str:
    try:
        text = path.read_text().strip()
    except OSError as exc:
        return f"could not read {path.name}: {exc}"
    return text or "unspecified"


# ---------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------


def _print_summary(report: ReconciliationReport, in_progress_count: int) -> None:
    """Emit a short `reconcile: ...` line for operator visibility.

    Per-repair lines are the orchestrator's responsibility — this
    module only describes what it observed, never what it changed.
    Merge-tracked tasks (skipped before classification) get one
    line each, alongside the per-finding lines.
    """
    task_noun = "task" if in_progress_count == 1 else "tasks"
    print(f"reconcile: {in_progress_count} in-progress {task_noun}")
    for task_id, pr_number in report.merge_tracked:
        print(
            f"reconcile:   {task_id} → skipped "
            f"(merge-tracked at PR #{pr_number})"
        )
    for finding in report.findings:
        print(f"reconcile:   {_describe(finding)}")


def _describe(finding: Finding) -> str:
    match finding:
        case MissingWorktree(task_id=task_id):
            return f"{task_id} → missing-worktree"
        case CompletionPending(task_id=task_id):
            return f"{task_id} → completion-pending"
        case CompletionPendingWithPr(task_id=task_id, pr_url=pr_url):
            return f"{task_id} → completion-pending-with-pr ({pr_url})"
        case FailurePending(task_id=task_id, reason=reason):
            return f"{task_id} → failure-pending ({reason})"
        case StaleNoSentinels(task_id=task_id):
            return f"{task_id} → stale-no-sentinels"
        case OrphanBranch(branch=branch):
            return f"{branch} → orphan-branch"
    return repr(finding)  # pragma: no cover — exhaustiveness guard
