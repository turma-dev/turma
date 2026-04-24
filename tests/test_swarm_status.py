"""Tests for the swarm status readout module.

The status readout is strictly read-only; the headline test asserts
zero calls to every mutating adapter surface (`claim_task`,
`close_task`, `fail_task`, `setup`, `cleanup`, `commit_all`,
`push_branch`, `open_pr`). Per-section rendering tests cover the
pinned output shape from
`openspec/changes/turma-status/design.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from turma.errors import PlanningError
from turma.swarm import SwarmServices
from turma.swarm.pull_request import PrSummary
from turma.swarm.status import status_readout
from turma.transcription.beads import BeadsTaskRef, BeadsTaskSnapshot


# ---------------------------------------------------------------------
# Stubs — every method tracked so the no-mutation invariant is
# testable, not assumed.
# ---------------------------------------------------------------------


@dataclass
class StubBeads:
    all_snapshots: tuple[BeadsTaskSnapshot, ...] = ()
    ready_tasks: tuple[BeadsTaskRef, ...] = ()
    in_progress_tasks: tuple[BeadsTaskRef, ...] = ()
    retries: dict[str, int] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)
    list_raises: PlanningError | None = None

    # Read-only surfaces used by status_readout.
    def list_feature_tasks_all_statuses(
        self, feature: str
    ) -> tuple[BeadsTaskSnapshot, ...]:
        self.calls.append(("list_feature_tasks_all_statuses", feature))
        if self.list_raises is not None:
            raise self.list_raises
        return self.all_snapshots

    def list_ready_tasks(self, feature: str) -> tuple[BeadsTaskRef, ...]:
        self.calls.append(("list_ready_tasks", feature))
        return self.ready_tasks

    def list_in_progress_tasks(
        self, feature: str
    ) -> tuple[BeadsTaskRef, ...]:
        self.calls.append(("list_in_progress_tasks", feature))
        return self.in_progress_tasks

    def retries_so_far(self, task_id: str) -> int:
        self.calls.append(("retries_so_far", task_id))
        return self.retries.get(task_id, 0)

    # Mutation surfaces — must never be called.
    def claim_task(self, task_id: str) -> None:
        self.calls.append(("claim_task", task_id))

    def close_task(self, task_id: str) -> None:
        self.calls.append(("close_task", task_id))

    def fail_task(self, *args, **kwargs) -> None:
        self.calls.append(("fail_task", args, kwargs))


@dataclass
class StubWorktree:
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

    # Mutation surfaces — must never be called.
    def setup(self, **kwargs):
        self.calls.append(("setup", kwargs))

    def cleanup(self, *args, **kwargs):
        self.calls.append(("cleanup", args, kwargs))


@dataclass
class StubGit:
    calls: list[tuple] = field(default_factory=list)

    # status_readout does not call GitAdapter at all, but we track
    # mutation surfaces here to pin that invariant.
    def status_is_dirty(self, worktree: Path) -> bool:
        self.calls.append(("status_is_dirty", worktree))
        return False

    def commit_all(self, worktree: Path, message: str) -> str:
        self.calls.append(("commit_all", worktree, message))
        return ""

    def push_branch(self, *args, **kwargs) -> None:
        self.calls.append(("push_branch", args, kwargs))


@dataclass
class StubPr:
    prs: tuple[PrSummary, ...] = ()
    list_raises: PlanningError | None = None
    calls: list[tuple] = field(default_factory=list)

    def list_prs_for_feature(
        self, feature: str, worktree_manager
    ) -> tuple[PrSummary, ...]:
        self.calls.append(("list_prs_for_feature", feature))
        if self.list_raises is not None:
            raise self.list_raises
        return self.prs

    # Mutation surfaces.
    def open_pr(self, *args, **kwargs) -> str:
        self.calls.append(("open_pr", args, kwargs))
        return ""

    # `find_open_pr_url_for_branch` is read-only; the status module
    # does not call it, but it exists on the real adapter.
    def find_open_pr_url_for_branch(self, branch: str) -> str | None:
        self.calls.append(("find_open_pr_url_for_branch", branch))
        return None


# ---------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------


def _scratch_feature(
    tmp_path: Path,
    feature: str = "oauth",
    approved: bool = True,
    transcribed: bool = True,
) -> Path:
    change_dir = tmp_path / "openspec" / "changes" / feature
    change_dir.mkdir(parents=True)
    if approved:
        (change_dir / "APPROVED").write_text("approved\n")
    if transcribed:
        (change_dir / "TRANSCRIBED.md").write_text("# transcribed\n")
    return change_dir


def _make_services(
    tmp_path: Path,
    *,
    beads: StubBeads | None = None,
    pr: StubPr | None = None,
    task_branches: tuple[str, ...] = (),
    max_retries: int = 1,
) -> tuple[SwarmServices, StubBeads, StubWorktree, StubGit, StubPr]:
    beads = beads or StubBeads()
    wt = StubWorktree(repo_root=tmp_path, task_branches=task_branches)
    git = StubGit()
    pr = pr or StubPr()
    services = SwarmServices(
        beads=beads,  # type: ignore[arg-type]
        worktree=wt,  # type: ignore[arg-type]
        git=git,  # type: ignore[arg-type]
        pr=pr,  # type: ignore[arg-type]
        worker_factory=lambda: object(),  # type: ignore[return-value]
        repo_root=tmp_path,
        max_retries=max_retries,
    )
    return services, beads, wt, git, pr


def _snap(
    task_id: str,
    *,
    status: str = "open",
    title: str = "t",
    labels: tuple[str, ...] = ("feature:oauth",),
) -> BeadsTaskSnapshot:
    return BeadsTaskSnapshot(
        id=task_id, title=title, labels=labels, status=status
    )


def _ref(task_id: str, *, title: str = "t") -> BeadsTaskRef:
    return BeadsTaskRef(
        id=task_id, title=title, labels=("feature:oauth",)
    )


# ---------------------------------------------------------------------
# Headline — no-mutation invariant
# ---------------------------------------------------------------------


def test_status_readout_never_calls_any_mutation_surface(
    tmp_path: Path,
) -> None:
    """Populate every section's fixture simultaneously and assert
    zero mutation calls on every stub adapter. Same discipline as
    the reconciliation module's headline test."""
    _scratch_feature(tmp_path)
    beads = StubBeads(
        all_snapshots=(
            _snap("bd-1", status="in_progress"),
            _snap("bd-2", status="open"),
            _snap(
                "bd-3",
                status="open",
                labels=("feature:oauth", NEEDS := "needs_human_review"),
            ),
            _snap("bd-4", status="closed"),
            _snap("bd-5", status="blocked"),
        ),
        ready_tasks=(_ref("bd-2"),),
        in_progress_tasks=(_ref("bd-1"),),
        retries={"bd-1": 0},
    )
    pr = StubPr(
        prs=(
            PrSummary(
                number=1,
                url="https://example/pr/1",
                state="OPEN",
                title="t",
                head_branch="task/oauth/bd-1",
            ),
        )
    )
    services, _, wt, git, _ = _make_services(
        tmp_path,
        beads=beads,
        pr=pr,
        task_branches=(
            "task/oauth/bd-1",
            "task/oauth/bd-orphan",
        ),
    )
    # Create the bd-1 worktree + a sentinel so the in-progress
    # rendering traverses that branch.
    worktree = tmp_path / ".worktrees" / "oauth" / "bd-1"
    worktree.mkdir(parents=True)
    (worktree / ".task_complete").write_text("DONE\n")

    status_readout("oauth", services=services, repo_root=tmp_path)

    mutating_bd = {"claim_task", "close_task", "fail_task"}
    mutating_wt = {"setup", "cleanup"}
    mutating_git = {"status_is_dirty", "commit_all", "push_branch"}
    mutating_pr = {"open_pr"}

    assert not any(c[0] in mutating_bd for c in beads.calls)
    assert not any(c[0] in mutating_wt for c in wt.calls)
    assert not any(c[0] in mutating_git for c in git.calls)
    assert not any(c[0] in mutating_pr for c in pr.calls)


# ---------------------------------------------------------------------
# Feature header / preflight
# ---------------------------------------------------------------------


def test_header_shows_approved_and_transcribed_when_both_present(
    tmp_path: Path,
) -> None:
    _scratch_feature(tmp_path, approved=True, transcribed=True)
    services, *_ = _make_services(tmp_path)

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "feature: oauth" in out
    assert "  spec: openspec/changes/oauth/" in out
    assert "  approved: yes" in out
    assert "  transcribed: yes" in out


def test_header_shows_missing_spec_dir_inline(tmp_path: Path) -> None:
    """No openspec/changes/<feature>/ directory at all → render
    `no` with the `turma plan` hint, but do NOT raise."""
    services, *_ = _make_services(tmp_path)

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "  spec: openspec/changes/oauth/ (not present" in out
    assert "turma plan --feature oauth" in out
    assert "  approved: no" in out
    assert "  transcribed: no" in out


def test_header_shows_plan_to_beads_hint_when_approved_but_not_transcribed(
    tmp_path: Path,
) -> None:
    _scratch_feature(tmp_path, approved=True, transcribed=False)
    services, *_ = _make_services(tmp_path)

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "  approved: yes" in out
    assert "  transcribed: no" in out
    assert "turma plan-to-beads --feature oauth" in out


# ---------------------------------------------------------------------
# Task counters
# ---------------------------------------------------------------------


def test_counter_block_buckets_by_status_and_label(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    beads = StubBeads(
        all_snapshots=(
            _snap("a", status="in_progress"),
            _snap("b", status="in_progress"),
            _snap("c", status="open"),  # ready
            _snap("d", status="open"),  # ready
            _snap("e", status="open"),  # dep-blocked (not in ready list)
            _snap("f", status="blocked"),
            _snap("g", status="deferred"),
            _snap("h", status="closed"),
            _snap("i", status="closed"),
            _snap("j", status="closed"),
            _snap(
                "k",
                status="open",
                labels=("feature:oauth", "needs_human_review"),
            ),
        ),
        ready_tasks=(_ref("c"), _ref("d")),
        in_progress_tasks=(_ref("a"), _ref("b")),
    )
    services, *_ = _make_services(tmp_path, beads=beads)

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "  ready:              2" in out
    assert "  in_progress:        2" in out
    assert "  blocked / deferred: 3" in out  # e (dep-blocked), f, g
    assert "  closed:             3" in out
    assert "  needs_human_review: 1" in out


def test_counter_block_counts_zero_when_feature_empty(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    services, *_ = _make_services(tmp_path)

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "  ready:              0" in out
    assert "  in_progress:        0" in out
    assert "  blocked / deferred: 0" in out
    assert "  closed:             0" in out
    assert "  needs_human_review: 0" in out


# ---------------------------------------------------------------------
# Ready section
# ---------------------------------------------------------------------


def test_ready_section_populated(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    beads = StubBeads(
        ready_tasks=(
            _ref("bd-1", title="Wire OAuth"),
            _ref("bd-2", title="Write tests"),
        )
    )
    services, *_ = _make_services(tmp_path, beads=beads)

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "ready tasks:" in out
    assert "  bd-1 — Wire OAuth" in out
    assert "  bd-2 — Write tests" in out


def test_ready_section_empty_renders_none(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    services, *_ = _make_services(tmp_path)

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "ready tasks:\n  (none)" in out


# ---------------------------------------------------------------------
# In-progress section
# ---------------------------------------------------------------------


def test_in_progress_section_renders_retries_and_absent_worktree(
    tmp_path: Path,
) -> None:
    _scratch_feature(tmp_path)
    beads = StubBeads(
        in_progress_tasks=(_ref("bd-1", title="Wire OAuth"),),
        retries={"bd-1": 1},
    )
    services, *_ = _make_services(tmp_path, beads=beads, max_retries=2)

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "in-progress tasks:" in out
    assert "  bd-1 — Wire OAuth" in out
    assert "    retries: 1 / 2" in out
    assert "(absent)" in out
    # No worktree, no sentinel.
    assert "    sentinel: none" in out


def test_in_progress_section_sentinel_complete(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    beads = StubBeads(
        in_progress_tasks=(_ref("bd-1"),),
    )
    services, *_ = _make_services(tmp_path, beads=beads)
    worktree = tmp_path / ".worktrees" / "oauth" / "bd-1"
    worktree.mkdir(parents=True)
    (worktree / ".task_complete").write_text("DONE\n")

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "(present)" in out
    assert "    sentinel: complete" in out


def test_in_progress_section_sentinel_failed_with_reason(
    tmp_path: Path,
) -> None:
    _scratch_feature(tmp_path)
    beads = StubBeads(
        in_progress_tasks=(_ref("bd-1"),),
    )
    services, *_ = _make_services(tmp_path, beads=beads)
    worktree = tmp_path / ".worktrees" / "oauth" / "bd-1"
    worktree.mkdir(parents=True)
    (worktree / ".task_failed").write_text(
        "intentional smoke failure\nextra context line\n"
    )

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    # First line only, truncated to keep readout compact.
    assert '    sentinel: failed: "intentional smoke failure"' in out
    assert "extra context line" not in out


def test_in_progress_section_sentinel_none_when_worktree_empty(
    tmp_path: Path,
) -> None:
    _scratch_feature(tmp_path)
    beads = StubBeads(
        in_progress_tasks=(_ref("bd-1"),),
    )
    services, *_ = _make_services(tmp_path, beads=beads)
    worktree = tmp_path / ".worktrees" / "oauth" / "bd-1"
    worktree.mkdir(parents=True)  # exists but no sentinels

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "(present)" in out
    assert "    sentinel: none" in out


def test_in_progress_section_empty_renders_none(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    services, *_ = _make_services(tmp_path)

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "in-progress tasks:\n  (none)" in out


# ---------------------------------------------------------------------
# Pull requests section
# ---------------------------------------------------------------------


def test_pull_requests_section_renders_mixed_states(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    pr = StubPr(
        prs=(
            PrSummary(
                number=7,
                url="https://github.com/o/r/pull/7",
                state="OPEN",
                title="[impl] Wire OAuth",
                head_branch="task/oauth/bd-1",
            ),
            PrSummary(
                number=6,
                url="https://github.com/o/r/pull/6",
                state="MERGED",
                title="[impl] Wire deps",
                head_branch="task/oauth/bd-2",
            ),
            PrSummary(
                number=5,
                url="https://github.com/o/r/pull/5",
                state="CLOSED",
                title="[impl] Reverted",
                head_branch="task/oauth/bd-3",
            ),
        )
    )
    services, *_ = _make_services(tmp_path, pr=pr)

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "pull requests:" in out
    assert "  #7 OPEN — [impl] Wire OAuth" in out
    assert "    head: task/oauth/bd-1" in out
    assert "    url:  https://github.com/o/r/pull/7" in out
    assert "  #6 MERGED — [impl] Wire deps" in out
    assert "  #5 CLOSED — [impl] Reverted" in out


def test_pull_requests_section_empty_renders_none(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    services, *_ = _make_services(tmp_path)

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "pull requests:\n  (none)" in out


# ---------------------------------------------------------------------
# Orphan branches section — strict in_progress-only filter
# ---------------------------------------------------------------------


def test_orphan_branches_section_in_progress_branch_not_rendered(
    tmp_path: Path,
) -> None:
    """A branch matching an in-progress task is NOT orphan — the
    in-progress section already surfaces that task."""
    _scratch_feature(tmp_path)
    beads = StubBeads(in_progress_tasks=(_ref("bd-1"),))
    services, *_ = _make_services(
        tmp_path,
        beads=beads,
        task_branches=("task/oauth/bd-1",),
    )

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "orphan branches:\n  (none)" in out


def test_orphan_branches_section_ready_task_branch_rendered(
    tmp_path: Path,
) -> None:
    """A ready-task branch IS rendered as orphan because the
    reconciliation contract defines orphan as "no in_progress task".
    The status readout matches that classification; it does not
    apply the repair-phase Option B log suppression here."""
    _scratch_feature(tmp_path)
    beads = StubBeads(ready_tasks=(_ref("bd-retry"),))
    services, *_ = _make_services(
        tmp_path,
        beads=beads,
        task_branches=("task/oauth/bd-retry",),
    )

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "  task/oauth/bd-retry  (no in_progress task)" in out


def test_orphan_branches_section_closed_task_branch_rendered(
    tmp_path: Path,
) -> None:
    """Cleanup-residue case: branch belongs to a task that's closed
    (not in_progress) — orphan per the contract."""
    _scratch_feature(tmp_path)
    beads = StubBeads(
        all_snapshots=(_snap("bd-done", status="closed"),),
    )
    services, *_ = _make_services(
        tmp_path,
        beads=beads,
        task_branches=("task/oauth/bd-done",),
    )

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "  task/oauth/bd-done  (no in_progress task)" in out


def test_orphan_branches_section_unaffiliated_branch_rendered(
    tmp_path: Path,
) -> None:
    """A branch with no corresponding task at all — orphan."""
    _scratch_feature(tmp_path)
    services, *_ = _make_services(
        tmp_path,
        task_branches=("task/oauth/bd-nobody-home",),
    )

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "  task/oauth/bd-nobody-home  (no in_progress task)" in out


def test_orphan_branches_section_empty_renders_none(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    services, *_ = _make_services(tmp_path)

    out = status_readout("oauth", services=services, repo_root=tmp_path)

    assert "orphan branches:\n  (none)" in out


# ---------------------------------------------------------------------
# Error propagation — adapter PlanningError is fatal (no partial readout)
# ---------------------------------------------------------------------


def test_adapter_planning_error_propagates_from_bd_list(
    tmp_path: Path,
) -> None:
    _scratch_feature(tmp_path)
    beads = StubBeads(
        list_raises=PlanningError("bd list failed: exit 1\nboom"),
    )
    services, *_ = _make_services(tmp_path, beads=beads)

    with pytest.raises(PlanningError, match="bd list failed"):
        status_readout("oauth", services=services, repo_root=tmp_path)


def test_adapter_planning_error_propagates_from_gh_list(
    tmp_path: Path,
) -> None:
    _scratch_feature(tmp_path)
    pr = StubPr(
        list_raises=PlanningError("gh pr list failed: exit 1\nboom"),
    )
    services, *_ = _make_services(tmp_path, pr=pr)

    with pytest.raises(PlanningError, match="gh pr list failed"):
        status_readout("oauth", services=services, repo_root=tmp_path)


# ---------------------------------------------------------------------
# Public-surface re-export
# ---------------------------------------------------------------------


def test_status_readout_is_reexported_from_turma_swarm() -> None:
    """The CLI will import `from turma.swarm import status_readout`
    in Task 5; pin the re-export now so a refactor of
    `turma.swarm.__init__.py` doesn't silently break that import."""
    from turma.swarm import status_readout as reexport_status_readout
    from turma.swarm.status import status_readout as direct_status_readout

    assert reexport_status_readout is direct_status_readout
