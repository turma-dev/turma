"""Tests for the WorktreeManager."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock, patch

import pytest

from turma.errors import PlanningError
from turma.swarm.worktree import (
    GIT_INSTALL_HINT,
    WorktreeManager,
    WorktreeRef,
)


def _completed(
    argv: list[str], stdout: str = "", stderr: str = "", code: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


def _make_manager_with_run(
    tmp_path: Path,
    run_fn: Callable[..., subprocess.CompletedProcess[str]],
    worktree_root: str = ".worktrees",
) -> WorktreeManager:
    with patch(
        "turma.swarm.worktree.shutil.which", return_value="/usr/bin/git"
    ):
        manager = WorktreeManager(
            repo_root=tmp_path, worktree_root=worktree_root
        )
    manager._run = run_fn  # type: ignore[method-assign]
    return manager


# -----------------------------------------------------------------------
# Construction
# -----------------------------------------------------------------------


@patch("turma.swarm.worktree.shutil.which", return_value="/usr/bin/git")
def test_init_succeeds_when_git_on_path(
    _which: MagicMock, tmp_path: Path
) -> None:
    WorktreeManager(repo_root=tmp_path)  # no exception


@patch("turma.swarm.worktree.shutil.which", return_value=None)
def test_init_raises_when_git_missing(
    _which: MagicMock, tmp_path: Path
) -> None:
    with pytest.raises(PlanningError) as exc:
        WorktreeManager(repo_root=tmp_path)
    assert GIT_INSTALL_HINT == str(exc.value)


# -----------------------------------------------------------------------
# setup — argv pinning
# -----------------------------------------------------------------------


def test_setup_new_worktree_pins_argv(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def run(argv, *, step):
        seen.append(argv)
        if argv[3:6] == ["worktree", "list", "--porcelain"]:
            return _completed(argv, stdout="")  # no existing worktrees
        if argv[3:5] == ["worktree", "add"]:
            return _completed(argv)
        raise AssertionError(f"unexpected argv: {argv}")

    manager = _make_manager_with_run(tmp_path, run)
    ref = manager.setup(
        feature="oauth",
        task_id="bd-smoke-1",
        base_branch="main",
    )

    # First call lists; second creates.
    assert seen[0] == [
        "git", "-C", str(tmp_path.resolve()),
        "worktree", "list", "--porcelain",
    ]
    expected_path = (tmp_path / ".worktrees" / "oauth" / "bd-smoke-1").resolve()
    assert seen[1] == [
        "git", "-C", str(tmp_path.resolve()),
        "worktree", "add",
        str(expected_path),
        "-b", "task/oauth/bd-smoke-1",
        "main",
    ]
    assert ref == WorktreeRef(
        path=expected_path,
        branch="task/oauth/bd-smoke-1",
    )


def test_setup_reuses_existing_worktree_without_add(tmp_path: Path) -> None:
    seen: list[list[str]] = []
    expected_path = (tmp_path / ".worktrees" / "oauth" / "bd-smoke-1").resolve()
    list_output = (
        f"worktree {expected_path}\n"
        f"HEAD 0123456789abcdef\n"
        f"branch refs/heads/task/oauth/bd-smoke-1\n"
        f"\n"
    )

    def run(argv, *, step):
        seen.append(argv)
        if argv[3:6] == ["worktree", "list", "--porcelain"]:
            return _completed(argv, stdout=list_output)
        raise AssertionError(
            f"setup should not run {argv} when the worktree is already registered"
        )

    manager = _make_manager_with_run(tmp_path, run)
    ref = manager.setup(
        feature="oauth",
        task_id="bd-smoke-1",
        base_branch="main",
    )

    assert len(seen) == 1  # only the list call; no `worktree add`
    assert ref.path == expected_path
    assert ref.branch == "task/oauth/bd-smoke-1"


def test_setup_raises_on_branch_collision(tmp_path: Path) -> None:
    def run(argv, *, step):
        if argv[3:6] == ["worktree", "list", "--porcelain"]:
            return _completed(argv, stdout="")
        if argv[3:5] == ["worktree", "add"]:
            raise PlanningError(
                "git worktree add failed: exit 128\n"
                "fatal: a branch named 'task/oauth/bd-smoke-1' already exists"
            )
        raise AssertionError(f"unexpected argv: {argv}")

    manager = _make_manager_with_run(tmp_path, run)
    with pytest.raises(PlanningError, match="already exists"):
        manager.setup(
            feature="oauth",
            task_id="bd-smoke-1",
            base_branch="main",
        )


def test_setup_resolves_relative_worktree_root_under_repo(
    tmp_path: Path,
) -> None:
    """A relative worktree_root is anchored at repo_root."""
    seen: list[list[str]] = []

    def run(argv, *, step):
        seen.append(argv)
        return _completed(argv, stdout="")

    manager = _make_manager_with_run(
        tmp_path, run, worktree_root=".custom-worktrees"
    )
    ref = manager.setup(
        feature="oauth",
        task_id="bd-1",
        base_branch="main",
    )

    expected = (tmp_path / ".custom-worktrees" / "oauth" / "bd-1").resolve()
    assert ref.path == expected
    # argv on the add call uses the same path.
    add_argv = next(a for a in seen if a[3:5] == ["worktree", "add"])
    assert add_argv[5] == str(expected)


def test_setup_accepts_absolute_worktree_root(
    tmp_path: Path,
) -> None:
    """An absolute worktree_root is used as-is, not re-anchored."""
    absolute_root = tmp_path / "elsewhere"

    def run(argv, *, step):
        return _completed(argv, stdout="")

    manager = _make_manager_with_run(
        tmp_path, run, worktree_root=str(absolute_root)
    )
    ref = manager.setup(
        feature="oauth",
        task_id="bd-1",
        base_branch="main",
    )

    assert ref.path == (absolute_root / "oauth" / "bd-1").resolve()


# -----------------------------------------------------------------------
# cleanup
# -----------------------------------------------------------------------


def test_cleanup_removes_worktree_then_deletes_branch(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def run(argv, *, step):
        seen.append(argv)
        return _completed(argv)

    manager = _make_manager_with_run(tmp_path, run)
    ref = WorktreeRef(
        path=tmp_path / ".worktrees" / "oauth" / "bd-1",
        branch="task/oauth/bd-1",
    )

    manager.cleanup(ref)

    assert seen == [
        [
            "git", "-C", str(tmp_path.resolve()),
            "worktree", "remove", "--force",
            str(ref.path),
        ],
        [
            "git", "-C", str(tmp_path.resolve()),
            "branch", "-D", "task/oauth/bd-1",
        ],
    ]


def test_cleanup_tolerates_missing_branch(tmp_path: Path) -> None:
    """`branch -D` after an already-deleted branch is not an error."""
    seen: list[list[str]] = []

    def run(argv, *, step):
        seen.append(argv)
        if argv[3:5] == ["worktree", "remove"]:
            return _completed(argv)
        if argv[3:5] == ["branch", "-D"]:
            raise PlanningError(
                "git branch -D failed: exit 1\n"
                "error: branch 'task/oauth/bd-1' not found."
            )
        raise AssertionError(f"unexpected argv: {argv}")

    manager = _make_manager_with_run(tmp_path, run)
    ref = WorktreeRef(
        path=tmp_path / ".worktrees" / "oauth" / "bd-1",
        branch="task/oauth/bd-1",
    )

    manager.cleanup(ref)  # does not raise

    # Both commands were attempted even though the second failed-soft.
    assert [a[3:5] for a in seen] == [
        ["worktree", "remove"],
        ["branch", "-D"],
    ]


def test_cleanup_surfaces_other_branch_delete_failures(
    tmp_path: Path,
) -> None:
    """A non-"branch not found" branch-delete failure bubbles up."""
    def run(argv, *, step):
        if argv[3:5] == ["worktree", "remove"]:
            return _completed(argv)
        if argv[3:5] == ["branch", "-D"]:
            raise PlanningError(
                "git branch -D failed: exit 1\n"
                "error: unmerged commits in 'task/oauth/bd-1'."
            )
        raise AssertionError(f"unexpected argv: {argv}")

    manager = _make_manager_with_run(tmp_path, run)
    ref = WorktreeRef(
        path=tmp_path / ".worktrees" / "oauth" / "bd-1",
        branch="task/oauth/bd-1",
    )

    with pytest.raises(PlanningError, match="unmerged commits"):
        manager.cleanup(ref)


def test_cleanup_surfaces_worktree_remove_failure(tmp_path: Path) -> None:
    """A failing `git worktree remove` always raises — triage state on disk."""
    def run(argv, *, step):
        if argv[3:5] == ["worktree", "remove"]:
            raise PlanningError(
                "git worktree remove failed: exit 128\n"
                "fatal: 'task/oauth/bd-1' contains modified or untracked files"
            )
        raise AssertionError(f"unexpected argv: {argv}")

    manager = _make_manager_with_run(tmp_path, run)
    ref = WorktreeRef(
        path=tmp_path / ".worktrees" / "oauth" / "bd-1",
        branch="task/oauth/bd-1",
    )

    with pytest.raises(PlanningError, match="modified or untracked files"):
        manager.cleanup(ref)


# -----------------------------------------------------------------------
# _run plumbing
# -----------------------------------------------------------------------


@patch("turma.swarm.worktree.shutil.which", return_value="/usr/bin/git")
@patch("turma.swarm.worktree.subprocess.run")
def test_run_surfaces_stderr_on_non_zero_exit(
    mock_run: MagicMock, _which: MagicMock, tmp_path: Path
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["git", "worktree", "add"],
        returncode=128,
        stdout="",
        stderr="fatal: something broke",
    )
    manager = WorktreeManager(repo_root=tmp_path)
    with pytest.raises(PlanningError) as exc:
        manager.setup(
            feature="oauth",
            task_id="bd-1",
            base_branch="main",
        )
    assert "fatal: something broke" in str(exc.value)


# -----------------------------------------------------------------------
# Branch name + path derivation
# -----------------------------------------------------------------------


def test_branch_name_is_deterministic() -> None:
    assert (
        WorktreeManager._branch_name("oauth", "bd-smoke-1")
        == "task/oauth/bd-smoke-1"
    )


def test_worktree_list_porcelain_detection_ignores_unrelated_entries(
    tmp_path: Path,
) -> None:
    """An unrelated worktree in `git worktree list` does not cause false reuse."""
    seen: list[list[str]] = []
    unrelated_path = tmp_path / ".worktrees" / "other-feature" / "bd-99"
    list_output = (
        f"worktree {unrelated_path}\n"
        f"HEAD 0123\n"
        f"branch refs/heads/task/other-feature/bd-99\n"
        f"\n"
    )

    def run(argv, *, step):
        seen.append(argv)
        if argv[3:6] == ["worktree", "list", "--porcelain"]:
            return _completed(argv, stdout=list_output)
        if argv[3:5] == ["worktree", "add"]:
            return _completed(argv)
        raise AssertionError(f"unexpected argv: {argv}")

    manager = _make_manager_with_run(tmp_path, run)
    manager.setup(
        feature="oauth",
        task_id="bd-smoke-1",
        base_branch="main",
    )

    # Worktree was created (the unrelated entry in the list did not
    # trigger the reuse path).
    assert any(a[3:5] == ["worktree", "add"] for a in seen)
