"""Tests for the swarm reconciliation module.

Reconciliation is read-only: it walks Beads in-progress state and the
worktree filesystem to produce a typed `ReconciliationReport`, never
mutating Beads / git / GitHub. These tests exercise each finding
category against stubs that track every method call so the read-only
invariant is checked, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from turma.swarm.reconciliation import (
    CompletionPending,
    CompletionPendingWithPr,
    FailurePending,
    MissingWorktree,
    OrphanBranch,
    ReconciliationReport,
    StaleNoSentinels,
    reconcile_feature,
)
from turma.transcription.beads import BeadsTaskRef


# ---------------------------------------------------------------------
# Stubs — track every call so the read-only invariant is checkable.
# ---------------------------------------------------------------------


@dataclass
class StubBeadsAdapter:
    in_progress: tuple[BeadsTaskRef, ...] = ()
    calls: list[tuple] = field(default_factory=list)

    def list_in_progress_tasks(
        self, feature: str
    ) -> tuple[BeadsTaskRef, ...]:
        self.calls.append(("list_in_progress_tasks", feature))
        return self.in_progress

    # Mutation surfaces — never called by reconciliation.
    def claim_task(self, task_id: str) -> None:
        self.calls.append(("claim_task", task_id))

    def close_task(self, task_id: str) -> None:
        self.calls.append(("close_task", task_id))

    def fail_task(self, *args, **kwargs) -> None:
        self.calls.append(("fail_task", args, kwargs))

    def retries_so_far(self, task_id: str) -> int:
        self.calls.append(("retries_so_far", task_id))
        return 0


@dataclass
class StubWorktreeManager:
    repo_root: Path
    task_branches: tuple[str, ...] = ()
    calls: list[tuple] = field(default_factory=list)

    def worktree_path_for(self, feature: str, task_id: str) -> Path:
        self.calls.append(("worktree_path_for", feature, task_id))
        return self.repo_root / ".worktrees" / feature / task_id

    def branch_name_for(self, feature: str, task_id: str) -> str:
        self.calls.append(("branch_name_for", feature, task_id))
        return f"task/{feature}/{task_id}"

    def list_task_branches(self, feature: str) -> tuple[str, ...]:
        self.calls.append(("list_task_branches", feature))
        return self.task_branches

    # Mutation surfaces — never called by reconciliation.
    def setup(self, **kwargs):
        self.calls.append(("setup", kwargs))

    def cleanup(self, *args, **kwargs):
        self.calls.append(("cleanup", args, kwargs))


@dataclass
class StubGitAdapter:
    calls: list[tuple] = field(default_factory=list)

    def status_is_dirty(self, worktree: Path) -> bool:
        self.calls.append(("status_is_dirty", worktree))
        return False

    def commit_all(self, worktree: Path, message: str) -> str:
        self.calls.append(("commit_all", worktree, message))
        return ""

    def push_branch(self, *args, **kwargs) -> None:
        self.calls.append(("push_branch", args, kwargs))


def _ref(task_id: str, title: str = "") -> BeadsTaskRef:
    return BeadsTaskRef(id=task_id, title=title, labels=("feature:oauth",))


def _make_worktree(
    repo_root: Path, feature: str, task_id: str
) -> Path:
    """Create an empty per-task worktree directory under `repo_root`."""
    path = repo_root / ".worktrees" / feature / task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reconcile(
    tmp_path: Path,
    *,
    in_progress: tuple[BeadsTaskRef, ...] = (),
    task_branches: tuple[str, ...] = (),
) -> tuple[ReconciliationReport, StubBeadsAdapter, StubWorktreeManager, StubGitAdapter]:
    bd = StubBeadsAdapter(in_progress=in_progress)
    wt = StubWorktreeManager(repo_root=tmp_path, task_branches=task_branches)
    git = StubGitAdapter()
    report = reconcile_feature(
        "oauth",
        adapter=bd,
        worktree_manager=wt,
        git_adapter=git,
        repo_root=tmp_path,
    )
    return report, bd, wt, git


# ---------------------------------------------------------------------
# Empty / happy path
# ---------------------------------------------------------------------


def test_no_in_progress_tasks_produces_empty_report(tmp_path: Path) -> None:
    report, bd, wt, git = _reconcile(tmp_path)
    assert report.findings == ()
    # Only the read methods were touched on any stub.
    assert bd.calls == [("list_in_progress_tasks", "oauth")]
    assert wt.calls == [("list_task_branches", "oauth")]
    assert git.calls == []


def test_returns_report_dataclass(tmp_path: Path) -> None:
    report, *_ = _reconcile(tmp_path)
    assert isinstance(report, ReconciliationReport)
    assert isinstance(report.findings, tuple)


# ---------------------------------------------------------------------
# missing-worktree
# ---------------------------------------------------------------------


def test_missing_worktree_finding(tmp_path: Path) -> None:
    """bd says in_progress, worktree directory absent."""
    report, bd, wt, git = _reconcile(
        tmp_path, in_progress=(_ref("bd-1"),)
    )
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert isinstance(finding, MissingWorktree)
    assert finding.task_id == "bd-1"
    assert finding.suggested_repair
    # Read-only invariant: no mutation stubs were called.
    assert all(c[0] != "close_task" for c in bd.calls)
    assert all(c[0] != "fail_task" for c in bd.calls)
    assert all(c[0] != "setup" for c in wt.calls)
    assert all(c[0] != "cleanup" for c in wt.calls)
    assert git.calls == []


# ---------------------------------------------------------------------
# completion-pending
# ---------------------------------------------------------------------


def test_completion_pending_finding_on_task_complete_sentinel(
    tmp_path: Path,
) -> None:
    worktree = _make_worktree(tmp_path, "oauth", "bd-2")
    (worktree / ".task_complete").write_text("DONE\n")

    report, bd, wt, git = _reconcile(
        tmp_path, in_progress=(_ref("bd-2"),)
    )
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert isinstance(finding, CompletionPending)
    assert finding.task_id == "bd-2"
    assert finding.suggested_repair
    # Read-only invariant.
    assert all(c[0] != "close_task" for c in bd.calls)
    assert git.calls == []


def test_completion_pending_wins_over_failure_when_both_sentinels_present(
    tmp_path: Path,
) -> None:
    """If both sentinels exist, completion wins (matches worker.py)."""
    worktree = _make_worktree(tmp_path, "oauth", "bd-3")
    (worktree / ".task_complete").write_text("DONE\n")
    (worktree / ".task_failed").write_text("oops\n")

    report, *_ = _reconcile(
        tmp_path, in_progress=(_ref("bd-3"),)
    )
    assert len(report.findings) == 1
    assert isinstance(report.findings[0], CompletionPending)


# ---------------------------------------------------------------------
# failure-pending
# ---------------------------------------------------------------------


def test_failure_pending_finding_surfaces_worker_reason(
    tmp_path: Path,
) -> None:
    worktree = _make_worktree(tmp_path, "oauth", "bd-4")
    (worktree / ".task_failed").write_text(
        "could not resolve import turma.foo\n"
    )

    report, bd, wt, git = _reconcile(
        tmp_path, in_progress=(_ref("bd-4"),)
    )
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert isinstance(finding, FailurePending)
    assert finding.task_id == "bd-4"
    assert "could not resolve import" in finding.reason
    # Read-only: fail_task is the repair phase's job, not reconciliation's.
    assert all(c[0] != "fail_task" for c in bd.calls)


def test_failure_pending_uses_unspecified_when_reason_blank(
    tmp_path: Path,
) -> None:
    worktree = _make_worktree(tmp_path, "oauth", "bd-5")
    (worktree / ".task_failed").write_text("   \n")

    report, *_ = _reconcile(
        tmp_path, in_progress=(_ref("bd-5"),)
    )
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert isinstance(finding, FailurePending)
    assert finding.reason == "unspecified"


# ---------------------------------------------------------------------
# stale-no-sentinels
# ---------------------------------------------------------------------


def test_stale_no_sentinels_when_worktree_present_but_empty(
    tmp_path: Path,
) -> None:
    _make_worktree(tmp_path, "oauth", "bd-6")  # no sentinels written

    report, bd, wt, git = _reconcile(
        tmp_path, in_progress=(_ref("bd-6"),)
    )
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert isinstance(finding, StaleNoSentinels)
    assert finding.task_id == "bd-6"
    # Read-only invariant still holds.
    assert all(c[0] != "close_task" for c in bd.calls)
    assert git.calls == []


# ---------------------------------------------------------------------
# orphan-branch
# ---------------------------------------------------------------------


def test_orphan_branch_finding_when_branch_has_no_in_progress_task(
    tmp_path: Path,
) -> None:
    report, bd, wt, git = _reconcile(
        tmp_path,
        in_progress=(),
        task_branches=("task/oauth/bd-old",),
    )
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert isinstance(finding, OrphanBranch)
    assert finding.branch == "task/oauth/bd-old"
    assert finding.suggested_repair


def test_in_progress_branch_is_not_treated_as_orphan(tmp_path: Path) -> None:
    """A branch matching an in_progress task id must not appear as orphan."""
    _make_worktree(tmp_path, "oauth", "bd-7")  # worktree present
    (tmp_path / ".worktrees" / "oauth" / "bd-7" / ".task_complete").write_text(
        "DONE\n"
    )

    report, *_ = _reconcile(
        tmp_path,
        in_progress=(_ref("bd-7"),),
        task_branches=("task/oauth/bd-7",),
    )
    assert len(report.findings) == 1
    assert isinstance(report.findings[0], CompletionPending)
    # No orphan alongside.
    assert not any(
        isinstance(f, OrphanBranch) for f in report.findings
    )


# ---------------------------------------------------------------------
# Composite scenarios
# ---------------------------------------------------------------------


def test_multiple_findings_preserve_in_progress_order(tmp_path: Path) -> None:
    """Findings over in_progress tasks are returned in iteration order,
    with orphan branches appended afterward."""
    worktree_b = _make_worktree(tmp_path, "oauth", "bd-b")
    (worktree_b / ".task_complete").write_text("DONE\n")
    worktree_c = _make_worktree(tmp_path, "oauth", "bd-c")
    (worktree_c / ".task_failed").write_text("blocker\n")

    report, *_ = _reconcile(
        tmp_path,
        in_progress=(_ref("bd-a"), _ref("bd-b"), _ref("bd-c")),
        task_branches=(
            "task/oauth/bd-a",  # will be skipped (matches in_progress task)
            "task/oauth/bd-b",  # skipped
            "task/oauth/bd-c",  # skipped
            "task/oauth/bd-orphan",  # orphan
        ),
    )
    kinds = [type(f).__name__ for f in report.findings]
    assert kinds == [
        "MissingWorktree",    # bd-a (no dir)
        "CompletionPending",  # bd-b
        "FailurePending",     # bd-c
        "OrphanBranch",       # bd-orphan
    ]


# ---------------------------------------------------------------------
# Read-only invariant (explicit, end-to-end)
# ---------------------------------------------------------------------


def test_reconciliation_never_calls_any_mutation_surface(
    tmp_path: Path,
) -> None:
    """Cover every finding category at once and assert zero mutations."""
    _make_worktree(tmp_path, "oauth", "bd-complete")
    (tmp_path / ".worktrees" / "oauth" / "bd-complete" / ".task_complete").write_text("DONE\n")
    _make_worktree(tmp_path, "oauth", "bd-fail")
    (tmp_path / ".worktrees" / "oauth" / "bd-fail" / ".task_failed").write_text("why\n")
    _make_worktree(tmp_path, "oauth", "bd-stale")  # no sentinels

    _, bd, wt, git = _reconcile(
        tmp_path,
        in_progress=(
            _ref("bd-missing"),    # no worktree dir
            _ref("bd-complete"),
            _ref("bd-fail"),
            _ref("bd-stale"),
        ),
        task_branches=("task/oauth/bd-orphan",),
    )
    mutating_bd_methods = {"claim_task", "close_task", "fail_task"}
    assert not any(c[0] in mutating_bd_methods for c in bd.calls)
    mutating_wt_methods = {"setup", "cleanup"}
    assert not any(c[0] in mutating_wt_methods for c in wt.calls)
    mutating_git_methods = {"commit_all", "push_branch"}
    assert not any(c[0] in mutating_git_methods for c in git.calls)


# ---------------------------------------------------------------------
# Deferred finding type — CompletionPendingWithPr
# ---------------------------------------------------------------------


def test_completion_pending_with_pr_is_importable_but_not_emitted(
    tmp_path: Path,
) -> None:
    """The dataclass exists for the repair phase (Task 7) to construct
    after disambiguating PR state. reconcile_feature itself never
    emits it in v1 — it has no PR adapter in its signature."""
    worktree = _make_worktree(tmp_path, "oauth", "bd-pr")
    (worktree / ".task_complete").write_text("DONE\n")

    report, *_ = _reconcile(
        tmp_path, in_progress=(_ref("bd-pr"),)
    )
    assert not any(
        isinstance(f, CompletionPendingWithPr) for f in report.findings
    )
    # But the type is importable and constructible (for the repair phase).
    finding = CompletionPendingWithPr(
        task_id="bd-pr",
        pr_url="https://github.com/example/repo/pull/1",
        suggested_repair="close the Beads task; remove the worktree",
    )
    assert finding.pr_url.endswith("/1")


# ---------------------------------------------------------------------
# Stdout summary
# ---------------------------------------------------------------------


def test_summary_prints_count_when_no_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _reconcile(tmp_path)
    captured = capsys.readouterr()
    assert "reconcile:" in captured.out
    assert "0 in-progress" in captured.out


def test_summary_prints_findings_breakdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    worktree = _make_worktree(tmp_path, "oauth", "bd-done")
    (worktree / ".task_complete").write_text("DONE\n")

    _reconcile(
        tmp_path,
        in_progress=(_ref("bd-done"),),
        task_branches=("task/oauth/bd-orphan",),
    )
    captured = capsys.readouterr()
    # One line per finding category or a rolled-up summary; in either
    # case the task id + branch should appear somewhere.
    assert "bd-done" in captured.out
    assert "bd-orphan" in captured.out or "orphan" in captured.out
