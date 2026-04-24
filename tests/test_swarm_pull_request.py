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
    PrSummary,
    PullRequestAdapter,
)


class _StubWorktreeManager:
    """Minimal stub carrying only the `branch_name_for` method
    `list_prs_for_feature` needs to derive the `task/<feature>/`
    prefix. Matches the real `WorktreeManager.branch_name_for`
    contract (feature + task_id → `task/<feature>/<task_id>`)."""

    def branch_name_for(self, feature: str, task_id: str) -> str:
        return f"task/{feature}/{task_id}"


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


# ---------------------------------------------------------------------
# find_open_pr_url_for_branch
# ---------------------------------------------------------------------


def test_find_open_pr_url_returns_url_when_one_match() -> None:
    adapter = _make_adapter()
    seen: list[list[str]] = []

    def fake_run(argv, **_):
        seen.append(argv)
        return _completed(
            argv,
            stdout='[{"url":"https://github.com/turma-dev/turma/pull/7"}]\n',
        )

    with patch(
        "turma.swarm.pull_request.subprocess.run", side_effect=fake_run
    ):
        url = adapter.find_open_pr_url_for_branch("task/oauth/bd-1")

    assert url == "https://github.com/turma-dev/turma/pull/7"
    assert seen == [
        [
            "gh", "pr", "list",
            "--head", "task/oauth/bd-1",
            "--state", "open",
            "--json", "url",
        ]
    ]


def test_find_open_pr_url_returns_none_on_empty_array() -> None:
    adapter = _make_adapter()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=_completed(["gh"], stdout="[]\n"),
    ):
        assert (
            adapter.find_open_pr_url_for_branch("task/oauth/bd-1")
            is None
        )


def test_find_open_pr_url_returns_none_on_empty_stdout() -> None:
    adapter = _make_adapter()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=_completed(["gh"], stdout=""),
    ):
        assert (
            adapter.find_open_pr_url_for_branch("task/oauth/bd-1")
            is None
        )


def test_find_open_pr_url_surfaces_stderr_on_non_zero_exit() -> None:
    adapter = _make_adapter()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["gh", "pr", "list"],
            returncode=1,
            stdout="",
            stderr="no git remote named 'origin'",
        ),
    ):
        with pytest.raises(PlanningError, match="no git remote"):
            adapter.find_open_pr_url_for_branch("task/oauth/bd-1")


def test_find_open_pr_url_rejects_non_json_output() -> None:
    adapter = _make_adapter()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=_completed(["gh"], stdout="definitely not json"),
    ):
        with pytest.raises(PlanningError, match="non-JSON output"):
            adapter.find_open_pr_url_for_branch("task/oauth/bd-1")


def test_find_open_pr_url_rejects_non_array_json() -> None:
    adapter = _make_adapter()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=_completed(["gh"], stdout='{"not": "array"}'),
    ):
        with pytest.raises(PlanningError, match="non-array JSON"):
            adapter.find_open_pr_url_for_branch("task/oauth/bd-1")


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


# ---------------------------------------------------------------------
# list_prs_for_feature — batched PR listing for `turma status`
# ---------------------------------------------------------------------


def test_list_prs_for_feature_pins_argv() -> None:
    adapter = _make_adapter()
    wt = _StubWorktreeManager()
    seen: list[list[str]] = []

    def fake_run(argv, **_):
        seen.append(argv)
        return _completed(argv, stdout="[]\n")

    with patch(
        "turma.swarm.pull_request.subprocess.run", side_effect=fake_run
    ):
        result = adapter.list_prs_for_feature("oauth", wt)

    assert result == ()
    assert seen == [
        [
            "gh", "pr", "list",
            "--search", "head:task/oauth/",
            "--state", "all",
            "--json", "number,url,state,title,headRefName",
            "--limit", "100",
        ]
    ]


def test_list_prs_for_feature_parses_mixed_state_payload() -> None:
    adapter = _make_adapter()
    wt = _StubWorktreeManager()
    payload = (
        '[{"number":4,"state":"OPEN",'
        '"title":"[impl] Append a line",'
        '"url":"https://github.com/o/r/pull/4",'
        '"headRefName":"task/oauth/bd-1"},'
        '{"number":3,"state":"MERGED",'
        '"title":"[impl] Wire config",'
        '"url":"https://github.com/o/r/pull/3",'
        '"headRefName":"task/oauth/bd-2"},'
        '{"number":2,"state":"CLOSED",'
        '"title":"[impl] Reverted",'
        '"url":"https://github.com/o/r/pull/2",'
        '"headRefName":"task/oauth/bd-3"}]'
    )

    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=_completed(["gh"], stdout=payload),
    ):
        result = adapter.list_prs_for_feature("oauth", wt)

    assert result == (
        PrSummary(
            number=4,
            url="https://github.com/o/r/pull/4",
            state="OPEN",
            title="[impl] Append a line",
            head_branch="task/oauth/bd-1",
        ),
        PrSummary(
            number=3,
            url="https://github.com/o/r/pull/3",
            state="MERGED",
            title="[impl] Wire config",
            head_branch="task/oauth/bd-2",
        ),
        PrSummary(
            number=2,
            url="https://github.com/o/r/pull/2",
            state="CLOSED",
            title="[impl] Reverted",
            head_branch="task/oauth/bd-3",
        ),
    )


def test_list_prs_for_feature_returns_empty_on_empty_array() -> None:
    adapter = _make_adapter()
    wt = _StubWorktreeManager()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=_completed(["gh"], stdout="[]\n"),
    ):
        assert adapter.list_prs_for_feature("oauth", wt) == ()


def test_list_prs_for_feature_returns_empty_on_empty_stdout() -> None:
    adapter = _make_adapter()
    wt = _StubWorktreeManager()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=_completed(["gh"], stdout=""),
    ):
        assert adapter.list_prs_for_feature("oauth", wt) == ()


def test_list_prs_for_feature_surfaces_stderr_on_non_zero_exit() -> None:
    adapter = _make_adapter()
    wt = _StubWorktreeManager()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["gh", "pr", "list"],
            returncode=1,
            stdout="",
            stderr="could not resolve to a Repository with the name",
        ),
    ):
        with pytest.raises(
            PlanningError, match="could not resolve to a Repository"
        ):
            adapter.list_prs_for_feature("oauth", wt)


def test_list_prs_for_feature_rejects_non_json_output() -> None:
    adapter = _make_adapter()
    wt = _StubWorktreeManager()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=_completed(["gh"], stdout="definitely not json"),
    ):
        with pytest.raises(PlanningError, match="non-JSON output"):
            adapter.list_prs_for_feature("oauth", wt)


def test_list_prs_for_feature_rejects_non_array_json() -> None:
    adapter = _make_adapter()
    wt = _StubWorktreeManager()
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=_completed(["gh"], stdout='{"not":"array"}'),
    ):
        with pytest.raises(PlanningError, match="non-array JSON"):
            adapter.list_prs_for_feature("oauth", wt)


def test_list_prs_for_feature_skips_non_dict_records() -> None:
    """Defensive: if gh ever returns an element that isn't a dict
    (shouldn't happen, but matches existing adapter tolerance),
    skip rather than KeyError."""
    adapter = _make_adapter()
    wt = _StubWorktreeManager()
    payload = (
        '[{"number":1,"state":"OPEN","title":"ok",'
        '"url":"https://github.com/o/r/pull/1",'
        '"headRefName":"task/oauth/bd-1"},'
        '"bare string that is not a PR record"]'
    )
    with patch(
        "turma.swarm.pull_request.subprocess.run",
        return_value=_completed(["gh"], stdout=payload),
    ):
        result = adapter.list_prs_for_feature("oauth", wt)
    assert len(result) == 1
    assert result[0].number == 1
