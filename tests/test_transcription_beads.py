"""Tests for the Beads CLI subprocess adapter."""

from __future__ import annotations

import json
import subprocess
from typing import Callable
from unittest.mock import MagicMock, patch

import pytest

from turma.errors import PlanningError
from turma.transcription.beads import (
    BEADS_INSTALL_HINT,
    BeadsAdapter,
    BeadsTaskRef,
    VALID_BD_TYPES,
)


def _completed(
    argv: list[str], stdout: str = "", stderr: str = "", code: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


# -----------------------------------------------------------------------
# Construction
# -----------------------------------------------------------------------


@patch("turma.transcription.beads.shutil.which", return_value="/usr/bin/bd")
def test_init_succeeds_when_bd_on_path(_which: MagicMock) -> None:
    BeadsAdapter()  # no exception


@patch("turma.transcription.beads.shutil.which", return_value=None)
def test_init_raises_when_bd_missing(_which: MagicMock) -> None:
    with pytest.raises(PlanningError) as exc:
        BeadsAdapter()
    assert "brew install beads" in str(exc.value)
    assert str(exc.value).strip().startswith("bd CLI not found")


def test_install_hint_mentions_brew_and_repo() -> None:
    # Guard against regressions in the install-hint wording the adapter
    # surfaces. Previous versions of the spec said `pip install beads`,
    # which is a different package entirely.
    assert "brew install beads" in BEADS_INSTALL_HINT
    assert "steveyegge/beads" in BEADS_INSTALL_HINT


# -----------------------------------------------------------------------
# create_task — happy paths
# -----------------------------------------------------------------------


def _make_adapter_with_run(
    run_fn: Callable[..., subprocess.CompletedProcess[str]],
) -> BeadsAdapter:
    with patch(
        "turma.transcription.beads.shutil.which", return_value="/usr/bin/bd"
    ):
        adapter = BeadsAdapter()
    # Replace the private runner with our stub. This keeps the test
    # focused on the argv shape and response-handling logic rather than
    # on subprocess plumbing.
    adapter._run = run_fn  # type: ignore[method-assign]
    return adapter


def test_create_task_pins_argv_shape() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], *, step: str) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        if argv[:2] == ["bd", "create"]:
            return _completed(argv, stdout="bd-42\n")
        raise AssertionError(f"unexpected argv: {argv}")

    adapter = _make_adapter_with_run(run)
    new_id = adapter.create_task(
        title="First task",
        description="feature tag plus subtasks\n",
        bd_type="task",
        priority=0,
        feature="critic-loop",
    )

    assert new_id == "bd-42"
    assert seen == [
        [
            "bd", "create",
            "--silent",
            "--type", "task",
            "--priority", "0",
            "--description", "feature tag plus subtasks\n",
            "--labels", "feature:critic-loop",
            "First task",
        ]
    ]


def test_create_task_includes_extra_labels_in_order() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], *, step: str) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return _completed(argv, stdout="bd-7\n")

    adapter = _make_adapter_with_run(run)
    adapter.create_task(
        title="T",
        description="d",
        bd_type="task",
        priority=2,
        feature="foo",
        extra_labels=("turma-type:impl", "round:1"),
    )

    labels_idx = seen[0].index("--labels") + 1
    assert seen[0][labels_idx] == (
        "feature:foo,turma-type:impl,round:1"
    )


def test_create_task_adds_dependency_per_blocker() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], *, step: str) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        if argv[:2] == ["bd", "create"]:
            return _completed(argv, stdout="bd-new\n")
        if argv[:3] == ["bd", "dep", "add"]:
            return _completed(argv, stdout="")
        raise AssertionError(f"unexpected argv: {argv}")

    adapter = _make_adapter_with_run(run)
    new_id = adapter.create_task(
        title="Dependent task",
        description="body",
        bd_type="task",
        priority=1,
        feature="foo",
        blocker_ids=("bd-1", "bd-2"),
    )

    assert new_id == "bd-new"
    dep_calls = [argv for argv in seen if argv[:3] == ["bd", "dep", "add"]]
    # Semantics: `bd dep add <blocked> <blocker>` — blocked depends on
    # blocker. The NEW id is the blocked; each blocker is passed in turn.
    assert dep_calls == [
        ["bd", "dep", "add", "bd-new", "bd-1"],
        ["bd", "dep", "add", "bd-new", "bd-2"],
    ]


# -----------------------------------------------------------------------
# create_task — validation
# -----------------------------------------------------------------------


def test_create_task_rejects_unknown_bd_type() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="bd-1\n")
    )
    with pytest.raises(PlanningError, match="unsupported bd task type"):
        adapter.create_task(
            title="T",
            description="d",
            bd_type="impl",  # parser-type, not a bd type
            priority=0,
            feature="foo",
        )


@pytest.mark.parametrize("bad", [-1, 5, 100])
def test_create_task_rejects_priority_out_of_range(bad: int) -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="bd-1\n")
    )
    with pytest.raises(PlanningError, match="priority out of range"):
        adapter.create_task(
            title="T",
            description="d",
            bd_type="task",
            priority=bad,
            feature="foo",
        )


def test_create_task_raises_when_bd_create_returns_empty_stdout() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="\n", stderr="weird")
    )
    with pytest.raises(PlanningError, match="bd create returned empty"):
        adapter.create_task(
            title="T",
            description="d",
            bd_type="task",
            priority=0,
            feature="foo",
        )


def test_valid_bd_types_matches_upstream_set() -> None:
    # Upstream set at time of writing. If bd ever renames/expands its
    # type list, this guard surfaces the drift as a failing test before
    # the adapter silently accepts or rejects values the CLI has
    # redefined.
    assert VALID_BD_TYPES == {
        "bug", "feature", "task", "epic", "chore", "decision"
    }


# -----------------------------------------------------------------------
# close_task
# -----------------------------------------------------------------------


def test_close_task_pins_argv() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], *, step: str) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return _completed(argv)

    adapter = _make_adapter_with_run(run)
    adapter.close_task("bd-42")
    assert seen == [["bd", "close", "bd-42"]]


# -----------------------------------------------------------------------
# list_feature_tasks
# -----------------------------------------------------------------------


def test_list_feature_tasks_pins_argv_and_parses_json() -> None:
    seen: list[list[str]] = []

    payload = json.dumps([
        {
            "id": "bd-1",
            "title": "First",
            "labels": ["feature:foo", "turma-type:impl"],
        },
        {
            "id": "bd-2",
            "title": "Second",
            "labels": ["feature:foo", "turma-type:test"],
        },
    ])

    def run(argv: list[str], *, step: str) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return _completed(argv, stdout=payload)

    adapter = _make_adapter_with_run(run)
    refs = adapter.list_feature_tasks("foo")

    assert seen == [
        [
            "bd", "list",
            "--label", "feature:foo",
            "--json",
            "--limit", "0",
        ]
    ]
    assert refs == (
        BeadsTaskRef(
            id="bd-1", title="First",
            labels=("feature:foo", "turma-type:impl"),
        ),
        BeadsTaskRef(
            id="bd-2", title="Second",
            labels=("feature:foo", "turma-type:test"),
        ),
    )


def test_list_feature_tasks_returns_empty_on_empty_stdout() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="")
    )
    assert adapter.list_feature_tasks("foo") == ()


def test_list_feature_tasks_returns_empty_on_empty_array() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="[]")
    )
    assert adapter.list_feature_tasks("foo") == ()


def test_list_feature_tasks_rejects_non_json_output() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="not json")
    )
    with pytest.raises(PlanningError, match="non-JSON output"):
        adapter.list_feature_tasks("foo")


def test_list_feature_tasks_rejects_non_array_json() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout='{"not": "an array"}')
    )
    with pytest.raises(PlanningError, match="non-array JSON"):
        adapter.list_feature_tasks("foo")


def test_list_feature_tasks_skips_records_missing_id() -> None:
    payload = json.dumps(
        [
            {"id": "bd-1", "title": "kept", "labels": []},
            {"title": "no id present"},
        ]
    )
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout=payload)
    )
    refs = adapter.list_feature_tasks("foo")
    assert [r.id for r in refs] == ["bd-1"]


# -----------------------------------------------------------------------
# subprocess plumbing (covers `_run` behavior end-to-end)
# -----------------------------------------------------------------------


@patch("turma.transcription.beads.shutil.which", return_value="/usr/bin/bd")
@patch("turma.transcription.beads.subprocess.run")
def test_run_surfaces_stderr_on_non_zero_exit(
    mock_run: MagicMock, _which: MagicMock
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["bd", "close", "bd-1"],
        returncode=2,
        stdout="",
        stderr="issue not found: bd-1",
    )
    adapter = BeadsAdapter()
    with pytest.raises(PlanningError) as exc:
        adapter.close_task("bd-1")
    assert "bd close failed" in str(exc.value)
    assert "issue not found: bd-1" in str(exc.value)


@patch("turma.transcription.beads.shutil.which", return_value="/usr/bin/bd")
@patch("turma.transcription.beads.subprocess.run")
def test_run_uses_stdout_when_stderr_empty(
    mock_run: MagicMock, _which: MagicMock
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["bd", "close", "bd-1"],
        returncode=1,
        stdout="something on stdout",
        stderr="",
    )
    adapter = BeadsAdapter()
    with pytest.raises(PlanningError) as exc:
        adapter.close_task("bd-1")
    assert "something on stdout" in str(exc.value)


@patch("turma.transcription.beads.shutil.which", return_value="/usr/bin/bd")
@patch("turma.transcription.beads.subprocess.run")
def test_run_falls_back_to_unknown_error_when_both_streams_empty(
    mock_run: MagicMock, _which: MagicMock
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["bd", "close", "bd-1"],
        returncode=1,
        stdout="",
        stderr="",
    )
    adapter = BeadsAdapter()
    with pytest.raises(PlanningError, match="unknown error"):
        adapter.close_task("bd-1")
