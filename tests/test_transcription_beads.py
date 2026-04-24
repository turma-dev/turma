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
    BeadsTaskSnapshot,
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


def test_create_task_leaves_labeled_orphan_when_dep_add_fails() -> None:
    """Orphan-on-dep-failure is a documented, recoverable condition.

    `bd create` is a separate call from each `bd dep add`; if the task
    is created successfully and a subsequent `bd dep add` fails, the
    new task already exists in Beads. The caller (Task 3's pipeline)
    catches the raised PlanningError and relies on orphan detection
    via the feature label to recover. This test pins that behavior:
    the adapter surfaces the underlying `bd dep add` stderr verbatim
    and does not attempt to close the new task itself.
    """
    seen: list[list[str]] = []

    def run(argv: list[str], *, step: str) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        if argv[:2] == ["bd", "create"]:
            return _completed(argv, stdout="bd-orphan\n")
        if argv[:3] == ["bd", "dep", "add"]:
            raise PlanningError(
                f"bd dep add failed: exit 3\nblocker bd-missing not found"
            )
        raise AssertionError(f"unexpected argv: {argv}")

    adapter = _make_adapter_with_run(run)
    with pytest.raises(PlanningError) as exc:
        adapter.create_task(
            title="Needs a blocker",
            description="body",
            bd_type="task",
            priority=2,
            feature="foo",
            blocker_ids=("bd-missing",),
        )

    assert "bd dep add failed" in str(exc.value)
    assert "blocker bd-missing not found" in str(exc.value)
    # bd create ran (task exists), then the dep add failed.
    assert seen[0][:2] == ["bd", "create"]
    assert seen[1][:3] == ["bd", "dep", "add"]
    # No second `bd create` or `bd close` call — the orphan is left in
    # place for Task 3's orphan-detection path to clean up.
    assert len([a for a in seen if a[:2] == ["bd", "create"]]) == 1
    assert not any(a[:2] == ["bd", "close"] for a in seen)


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


# -----------------------------------------------------------------------
# get_task_body
# -----------------------------------------------------------------------


def test_get_task_body_pins_argv_and_returns_description() -> None:
    seen: list[list[str]] = []
    payload = json.dumps(
        {
            "id": "bd-1",
            "title": "t",
            "description": "line 1\nline 2\n",
        }
    )

    def run(argv, *, step):
        seen.append(argv)
        return _completed(argv, stdout=payload)

    adapter = _make_adapter_with_run(run)
    body = adapter.get_task_body("bd-1")

    assert body == "line 1\nline 2\n"
    assert seen == [["bd", "show", "bd-1", "--json"]]


def test_get_task_body_unwraps_single_element_list() -> None:
    payload = json.dumps([{"id": "bd-1", "description": "hello"}])
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout=payload)
    )
    assert adapter.get_task_body("bd-1") == "hello"


def test_get_task_body_returns_empty_when_stdout_blank() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="")
    )
    assert adapter.get_task_body("bd-1") == ""


def test_get_task_body_falls_back_to_body_field() -> None:
    payload = json.dumps({"id": "bd-1", "body": "from body"})
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout=payload)
    )
    assert adapter.get_task_body("bd-1") == "from body"


def test_get_task_body_rejects_non_json() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="not json")
    )
    with pytest.raises(PlanningError, match="non-JSON output"):
        adapter.get_task_body("bd-1")


# -----------------------------------------------------------------------
# list_in_progress_tasks
# -----------------------------------------------------------------------


def test_list_in_progress_tasks_pins_argv_and_parses_json() -> None:
    seen: list[list[str]] = []
    payload = json.dumps(
        [
            {
                "id": "bd-5",
                "title": "Claimed earlier",
                "labels": ["feature:oauth", "turma-retries:1"],
            }
        ]
    )

    def run(argv: list[str], *, step: str) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return _completed(argv, stdout=payload)

    adapter = _make_adapter_with_run(run)
    refs = adapter.list_in_progress_tasks("oauth")

    assert seen == [
        [
            "bd", "list",
            "--status", "in_progress",
            "--label", "feature:oauth",
            "--json",
            "--limit", "0",
        ]
    ]
    assert refs == (
        BeadsTaskRef(
            id="bd-5",
            title="Claimed earlier",
            labels=("feature:oauth", "turma-retries:1"),
        ),
    )


def test_list_in_progress_tasks_returns_empty_on_empty_stdout() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="")
    )
    assert adapter.list_in_progress_tasks("oauth") == ()


def test_list_in_progress_tasks_rejects_non_json_output() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="not json")
    )
    with pytest.raises(PlanningError, match="non-JSON output"):
        adapter.list_in_progress_tasks("oauth")


def test_list_in_progress_tasks_rejects_non_array_json() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout='{"not": "an array"}')
    )
    with pytest.raises(PlanningError, match="non-array JSON"):
        adapter.list_in_progress_tasks("oauth")


# -----------------------------------------------------------------------
# list_feature_tasks_all_statuses
# -----------------------------------------------------------------------


def test_list_feature_tasks_all_statuses_pins_argv_and_parses_mixed_payload() -> None:
    """`bd list --all` returns every feature-tagged task regardless of
    status. Used by `turma status` to build the counter block +
    catch closed-task cleanup residues the single-status listers
    miss. Each row carries its `status` so downstream bucketing can
    distinguish closed from in_progress from open etc."""
    seen: list[list[str]] = []
    payload = json.dumps(
        [
            {
                "id": "bd-1",
                "title": "In flight",
                "labels": ["feature:oauth", "turma-retries:1"],
                "status": "in_progress",
            },
            {
                "id": "bd-2",
                "title": "Awaiting review",
                "labels": ["feature:oauth", "needs_human_review"],
                "status": "open",
            },
            {
                "id": "bd-3",
                "title": "Done",
                "labels": ["feature:oauth", "turma-type:impl"],
                "status": "closed",
            },
        ]
    )

    def run(argv: list[str], *, step: str) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return _completed(argv, stdout=payload)

    adapter = _make_adapter_with_run(run)
    snapshots = adapter.list_feature_tasks_all_statuses("oauth")

    assert seen == [
        [
            "bd", "list",
            "--all",
            "--label", "feature:oauth",
            "--json",
            "--limit", "0",
        ]
    ]
    assert snapshots == (
        BeadsTaskSnapshot(
            id="bd-1",
            title="In flight",
            labels=("feature:oauth", "turma-retries:1"),
            status="in_progress",
        ),
        BeadsTaskSnapshot(
            id="bd-2",
            title="Awaiting review",
            labels=("feature:oauth", "needs_human_review"),
            status="open",
        ),
        BeadsTaskSnapshot(
            id="bd-3",
            title="Done",
            labels=("feature:oauth", "turma-type:impl"),
            status="closed",
        ),
    )


def test_list_feature_tasks_all_statuses_status_populated_across_bd_vocabulary() -> None:
    """Explicit pin that every value in bd 1.0.2's documented status
    vocabulary (`open | in_progress | blocked | deferred | closed`)
    flows through unchanged onto `BeadsTaskSnapshot.status`. Future
    bd drift adding a new status would pass through here too; the
    test would still pass but the downstream counter block's
    bucketing (Task 3) is what would need updating."""
    payload = json.dumps(
        [
            {"id": "a", "title": "a", "labels": [], "status": "open"},
            {"id": "b", "title": "b", "labels": [], "status": "in_progress"},
            {"id": "c", "title": "c", "labels": [], "status": "blocked"},
            {"id": "d", "title": "d", "labels": [], "status": "deferred"},
            {"id": "e", "title": "e", "labels": [], "status": "closed"},
        ]
    )
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout=payload)
    )
    snapshots = adapter.list_feature_tasks_all_statuses("oauth")
    assert [s.status for s in snapshots] == [
        "open", "in_progress", "blocked", "deferred", "closed",
    ]


def test_list_feature_tasks_all_statuses_tolerates_missing_status_field() -> None:
    """bd 1.0.2 always emits `status`, but if a payload ever omits
    the field the adapter should default to `""` rather than
    KeyError. Protects against bd schema drift in either direction."""
    payload = json.dumps([{"id": "bd-1", "title": "no status", "labels": []}])
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout=payload)
    )
    snapshots = adapter.list_feature_tasks_all_statuses("oauth")
    assert snapshots == (
        BeadsTaskSnapshot(
            id="bd-1", title="no status", labels=(), status=""
        ),
    )


def test_list_feature_tasks_all_statuses_returns_empty_on_empty_stdout() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="")
    )
    assert adapter.list_feature_tasks_all_statuses("oauth") == ()


def test_list_feature_tasks_all_statuses_returns_empty_on_empty_array() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="[]")
    )
    assert adapter.list_feature_tasks_all_statuses("oauth") == ()


def test_list_feature_tasks_all_statuses_rejects_non_json_output() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="not json")
    )
    with pytest.raises(PlanningError, match="non-JSON output"):
        adapter.list_feature_tasks_all_statuses("oauth")


def test_list_feature_tasks_all_statuses_rejects_non_array_json() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout='{"not": "an array"}')
    )
    with pytest.raises(PlanningError, match="non-array JSON"):
        adapter.list_feature_tasks_all_statuses("oauth")


def test_list_feature_tasks_all_statuses_skips_records_missing_id() -> None:
    payload = json.dumps(
        [
            {"id": "bd-1", "title": "kept", "labels": []},
            {"title": "no id present", "status": "closed"},
        ]
    )
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout=payload)
    )
    refs = adapter.list_feature_tasks_all_statuses("oauth")
    assert [r.id for r in refs] == ["bd-1"]


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
