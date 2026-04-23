"""Tests for the PullRequestAdapter subprocess wrapper."""

from __future__ import annotations

import subprocess
from typing import Callable
from unittest.mock import MagicMock, patch

import pytest

from turma.errors import PlanningError
from turma.swarm.pull_request import (
    GH_AUTH_HINT,
    GH_INSTALL_HINT,
    PullRequestAdapter,
)


def _completed(
    argv: list[str], stdout: str = "", stderr: str = "", code: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


def _make_adapter(
    auth_status: subprocess.CompletedProcess[str] | None = None,
) -> PullRequestAdapter:
    """Build a PullRequestAdapter past the init preflight.

    `gh` is forced onto PATH and `gh auth status` is stubbed to a
    zero-exit completion unless the caller overrides it.
    """
    if auth_status is None:
        auth_status = _completed(["gh", "auth", "status"])
    with patch(
        "turma.swarm.pull_request.shutil.which", return_value="/usr/bin/gh"
    ), patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=auth_status,
    ):
        return PullRequestAdapter()


# ---------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------


def test_init_succeeds_when_gh_on_path_and_authenticated() -> None:
    _make_adapter()  # no exception


@patch("turma.swarm.pull_request.shutil.which", return_value=None)
def test_init_raises_when_gh_missing(_which: MagicMock) -> None:
    with pytest.raises(PlanningError) as exc:
        PullRequestAdapter()
    assert str(exc.value) == GH_INSTALL_HINT


@patch("turma.swarm.pull_request.shutil.which", return_value="/usr/bin/gh")
@patch("turma.swarm.pull_request.subprocess.run")
def test_init_raises_when_gh_auth_status_fails(
    mock_run: MagicMock, _which: MagicMock
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["gh", "auth", "status"],
        returncode=1,
        stdout="",
        stderr="You are not logged into any GitHub hosts.",
    )
    with pytest.raises(PlanningError) as exc:
        PullRequestAdapter()
    msg = str(exc.value)
    assert GH_AUTH_HINT in msg
    assert "not logged into any GitHub hosts" in msg


@patch("turma.swarm.pull_request.shutil.which", return_value="/usr/bin/gh")
@patch("turma.swarm.pull_request.subprocess.run")
def test_init_runs_gh_auth_status_exactly_once(
    mock_run: MagicMock, _which: MagicMock
) -> None:
    """__init__ should preflight auth once; open_pr must not re-check."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=["gh", "auth", "status"],
        returncode=0,
        stdout="Logged in to github.com as user",
        stderr="",
    )
    PullRequestAdapter()
    assert mock_run.call_count == 1
    called_argv = mock_run.call_args_list[0].args[0]
    assert called_argv == ["gh", "auth", "status"]


# ---------------------------------------------------------------------
# open_pr — argv shape and URL parsing
# ---------------------------------------------------------------------


def test_open_pr_pins_argv_and_parses_url() -> None:
    adapter = _make_adapter()
    seen: list[list[str]] = []

    def fake_run(
        argv: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return _completed(
            argv,
            stdout="https://github.com/turma-dev/turma/pull/42\n",
        )

    with patch(
        "turma.swarm.pull_request.subprocess.run", side_effect=fake_run
    ):
        url = adapter.open_pr(
            branch="task/oauth/bd-smoke-1",
            base="main",
            title="[impl] Wire OAuth",
            body="Closes bd-smoke-1.\n\nBody text.",
        )

    assert url == "https://github.com/turma-dev/turma/pull/42"
    assert seen == [
        [
            "gh", "pr", "create",
            "--head", "task/oauth/bd-smoke-1",
            "--base", "main",
            "--title", "[impl] Wire OAuth",
            "--body", "Closes bd-smoke-1.\n\nBody text.",
        ]
    ]


def test_open_pr_extracts_url_when_preceded_by_hint_lines() -> None:
    """gh often prints a remote-hint line before the URL."""
    adapter = _make_adapter()
    hint_then_url = (
        "Creating pull request for task/oauth/bd-1 into main in "
        "turma-dev/turma\n"
        "\n"
        "https://github.com/turma-dev/turma/pull/99\n"
    )
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=_completed(["gh"], stdout=hint_then_url),
    ):
        url = adapter.open_pr(
            branch="task/oauth/bd-1",
            base="main",
            title="t",
            body="b",
        )
    assert url == "https://github.com/turma-dev/turma/pull/99"


def test_open_pr_raises_when_stdout_has_no_url() -> None:
    adapter = _make_adapter()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=_completed(["gh"], stdout="something unexpected\n"),
    ):
        with pytest.raises(PlanningError, match="no PR URL"):
            adapter.open_pr(
                branch="task/oauth/bd-1",
                base="main",
                title="t",
                body="b",
            )


# ---------------------------------------------------------------------
# open_pr — failure surface
# ---------------------------------------------------------------------


def test_open_pr_surfaces_gh_stderr_on_non_zero_exit() -> None:
    """A PAT-scope failure or policy rejection surfaces gh stderr verbatim."""
    adapter = _make_adapter()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=1,
            stdout="",
            stderr=(
                "pull request create failed: GraphQL: "
                "Resource not accessible by personal access token "
                "(createPullRequest)"
            ),
        ),
    ):
        with pytest.raises(PlanningError) as exc:
            adapter.open_pr(
                branch="task/oauth/bd-1",
                base="main",
                title="t",
                body="b",
            )

    msg = str(exc.value)
    assert "gh pr create failed" in msg
    assert "Resource not accessible by personal access token" in msg


def test_open_pr_falls_back_to_stdout_when_stderr_empty_on_failure() -> None:
    adapter = _make_adapter()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=1,
            stdout="stdout-only detail",
            stderr="",
        ),
    ):
        with pytest.raises(PlanningError, match="stdout-only detail"):
            adapter.open_pr(
                branch="task/oauth/bd-1",
                base="main",
                title="t",
                body="b",
            )


def test_open_pr_falls_back_to_unknown_error_when_both_empty() -> None:
    adapter = _make_adapter()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=1,
            stdout="",
            stderr="",
        ),
    ):
        with pytest.raises(PlanningError, match="unknown error"):
            adapter.open_pr(
                branch="task/oauth/bd-1",
                base="main",
                title="t",
                body="b",
            )
