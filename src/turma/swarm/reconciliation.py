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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from turma.transcription.beads import BeadsTaskRef


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
    """Ordered set of findings from a single reconciliation pass."""

    findings: tuple[Finding, ...]


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

    claimed_branches: set[str] = set()
    for task in in_progress:
        branch = worktree_manager.branch_name_for(feature, task.id)
        claimed_branches.add(branch)
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

    report = ReconciliationReport(findings=tuple(findings))
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
    """
    task_noun = "task" if in_progress_count == 1 else "tasks"
    print(f"reconcile: {in_progress_count} in-progress {task_noun}")
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
