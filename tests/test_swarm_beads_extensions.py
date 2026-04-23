"""Tests for the swarm-orchestration additions to BeadsAdapter."""

from __future__ import annotations

import json
import subprocess
from typing import Callable
from unittest.mock import patch

import pytest

from turma.errors import PlanningError
from turma.transcription.beads import (
    NEEDS_HUMAN_REVIEW_LABEL,
    RETRIES_LABEL_PREFIX,
    BeadsAdapter,
    BeadsTaskRef,
    _parse_retries_from_labels,
)


def _completed(
    argv: list[str], stdout: str = "", stderr: str = "", code: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


def _make_adapter_with_run(
    run_fn: Callable[..., subprocess.CompletedProcess[str]],
) -> BeadsAdapter:
    with patch(
        "turma.transcription.beads.shutil.which", return_value="/usr/bin/bd"
    ):
        adapter = BeadsAdapter()
    adapter._run = run_fn  # type: ignore[method-assign]
    return adapter


# -----------------------------------------------------------------------
# Label conventions
# -----------------------------------------------------------------------


def test_retries_label_prefix_and_needs_review_constants_are_pinned() -> None:
    # If upstream bd ever renames these or Turma reshapes the label
    # convention, this test fails loud and all call sites are easy to
    # find via the constant references.
    assert RETRIES_LABEL_PREFIX == "turma-retries:"
    assert NEEDS_HUMAN_REVIEW_LABEL == "needs_human_review"


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ([], 0),
        (["feature:foo"], 0),
        (["feature:foo", "turma-retries:0"], 0),
        (["feature:foo", "turma-retries:1"], 1),
        (["turma-retries:5", "feature:foo"], 5),
        (["turma-retries:not-a-number"], 0),  # ignored, fall through
        (["turma-retries:2", "turma-retries:9"], 2),  # first wins
    ],
)
def test_parse_retries_from_labels(labels, expected) -> None:
    assert _parse_retries_from_labels(labels) == expected


# -----------------------------------------------------------------------
# list_ready_tasks
# -----------------------------------------------------------------------


def test_list_ready_tasks_pins_argv_and_parses_json() -> None:
    seen: list[list[str]] = []
    payload = json.dumps([
        {
            "id": "bd-1",
            "title": "First ready",
            "labels": ["feature:oauth", "turma-type:impl"],
        },
        {
            "id": "bd-2",
            "title": "Second ready",
            "labels": ["feature:oauth", "turma-type:test"],
        },
    ])

    def run(argv, *, step):
        seen.append(argv)
        return _completed(argv, stdout=payload)

    adapter = _make_adapter_with_run(run)
    refs = adapter.list_ready_tasks("oauth")

    assert seen == [
        [
            "bd", "ready",
            "--label", "feature:oauth",
            "--json",
            "--limit", "0",
        ]
    ]
    assert refs == (
        BeadsTaskRef(
            id="bd-1", title="First ready",
            labels=("feature:oauth", "turma-type:impl"),
        ),
        BeadsTaskRef(
            id="bd-2", title="Second ready",
            labels=("feature:oauth", "turma-type:test"),
        ),
    )


def test_list_ready_tasks_excludes_needs_human_review() -> None:
    payload = json.dumps([
        {
            "id": "bd-1",
            "title": "Healthy",
            "labels": ["feature:oauth", "turma-type:impl"],
        },
        {
            "id": "bd-99",
            "title": "Stuck in human-review",
            "labels": ["feature:oauth", "needs_human_review"],
        },
        {
            "id": "bd-2",
            "title": "Also healthy",
            "labels": ["feature:oauth", "turma-type:test"],
        },
    ])
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout=payload)
    )

    refs = adapter.list_ready_tasks("oauth")
    assert [r.id for r in refs] == ["bd-1", "bd-2"]


def test_list_ready_tasks_returns_empty_on_empty_stdout() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="")
    )
    assert adapter.list_ready_tasks("oauth") == ()


def test_list_ready_tasks_returns_empty_on_empty_array() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="[]")
    )
    assert adapter.list_ready_tasks("oauth") == ()


def test_list_ready_tasks_rejects_non_json_output() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="not json")
    )
    with pytest.raises(PlanningError, match="non-JSON output"):
        adapter.list_ready_tasks("oauth")


def test_list_ready_tasks_rejects_non_array_json() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(
            argv, stdout='{"not": "an array"}'
        )
    )
    with pytest.raises(PlanningError, match="non-array JSON"):
        adapter.list_ready_tasks("oauth")


def test_list_ready_tasks_skips_records_missing_id() -> None:
    payload = json.dumps([
        {"id": "bd-1", "title": "kept", "labels": []},
        {"title": "no id"},
    ])
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout=payload)
    )
    refs = adapter.list_ready_tasks("oauth")
    assert [r.id for r in refs] == ["bd-1"]


# -----------------------------------------------------------------------
# claim_task
# -----------------------------------------------------------------------


def test_claim_task_pins_argv() -> None:
    seen: list[list[str]] = []

    def run(argv, *, step):
        seen.append(argv)
        return _completed(argv)

    adapter = _make_adapter_with_run(run)
    adapter.claim_task("bd-42")

    assert seen == [["bd", "update", "bd-42", "--claim"]]


def test_claim_task_surfaces_race_failure() -> None:
    """A claim race (task already claimed by another actor) surfaces bd stderr."""
    def run(argv, *, step):
        raise PlanningError(
            "bd update --claim failed: exit 1\n"
            "task already claimed by alice@example.com"
        )

    adapter = _make_adapter_with_run(run)
    with pytest.raises(PlanningError, match="already claimed"):
        adapter.claim_task("bd-42")


# -----------------------------------------------------------------------
# retries_so_far
# -----------------------------------------------------------------------


def test_retries_so_far_returns_zero_when_label_absent() -> None:
    payload = json.dumps({
        "id": "bd-1",
        "labels": ["feature:oauth", "turma-type:impl"],
    })
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout=payload)
    )
    assert adapter.retries_so_far("bd-1") == 0


def test_retries_so_far_parses_label() -> None:
    payload = json.dumps({
        "id": "bd-1",
        "labels": ["feature:oauth", "turma-retries:3"],
    })
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout=payload)
    )
    assert adapter.retries_so_far("bd-1") == 3


def test_retries_so_far_pins_argv() -> None:
    seen: list[list[str]] = []

    def run(argv, *, step):
        seen.append(argv)
        return _completed(argv, stdout='{"id": "bd-1", "labels": []}')

    adapter = _make_adapter_with_run(run)
    adapter.retries_so_far("bd-1")
    assert seen == [["bd", "show", "bd-1", "--json"]]


def test_retries_so_far_handles_array_response() -> None:
    # bd show --json can return a list with one element depending on
    # the exact invocation. Handle both shapes.
    payload = json.dumps([{"id": "bd-1", "labels": ["turma-retries:2"]}])
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout=payload)
    )
    assert adapter.retries_so_far("bd-1") == 2


def test_retries_so_far_returns_zero_on_empty_stdout() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="")
    )
    assert adapter.retries_so_far("bd-1") == 0


def test_retries_so_far_rejects_non_json() -> None:
    adapter = _make_adapter_with_run(
        lambda argv, *, step: _completed(argv, stdout="plain text")
    )
    with pytest.raises(PlanningError, match="non-JSON output"):
        adapter.retries_so_far("bd-1")


# -----------------------------------------------------------------------
# fail_task
# -----------------------------------------------------------------------


def test_fail_task_first_failure_adds_retries_1_without_remove() -> None:
    seen: list[list[str]] = []

    def run(argv, *, step):
        seen.append(argv)
        return _completed(argv)

    adapter = _make_adapter_with_run(run)
    adapter.fail_task(
        "bd-1",
        "flaky test",
        retries_so_far=0,
        max_retries=2,
    )

    assert seen == [
        [
            "bd", "update", "bd-1",
            "--append-notes", "flaky test",
            "--add-label", "turma-retries:1",
            "--status", "open",
        ]
    ]


def test_fail_task_within_budget_swaps_retries_label() -> None:
    seen: list[list[str]] = []

    def run(argv, *, step):
        seen.append(argv)
        return _completed(argv)

    adapter = _make_adapter_with_run(run)
    adapter.fail_task(
        "bd-1",
        "second failure",
        retries_so_far=1,
        max_retries=3,
    )

    assert seen == [
        [
            "bd", "update", "bd-1",
            "--append-notes", "second failure",
            "--remove-label", "turma-retries:1",
            "--add-label", "turma-retries:2",
            "--status", "open",
        ]
    ]


def test_fail_task_budget_exhaustion_swaps_in_needs_human_review() -> None:
    seen: list[list[str]] = []

    def run(argv, *, step):
        seen.append(argv)
        return _completed(argv)

    adapter = _make_adapter_with_run(run)
    adapter.fail_task(
        "bd-1",
        "final failure",
        retries_so_far=2,  # one more attempt would be N=3 > max_retries=2
        max_retries=2,
    )

    assert seen == [
        [
            "bd", "update", "bd-1",
            "--append-notes", "final failure",
            "--remove-label", "turma-retries:2",
            "--add-label", "needs_human_review",
            "--status", "open",
        ]
    ]


def test_fail_task_exhaustion_from_first_attempt_when_max_retries_zero() -> None:
    """max_retries=0 disables retry: first failure goes straight to needs_human_review."""
    seen: list[list[str]] = []

    def run(argv, *, step):
        seen.append(argv)
        return _completed(argv)

    adapter = _make_adapter_with_run(run)
    adapter.fail_task(
        "bd-1",
        "only chance",
        retries_so_far=0,
        max_retries=0,
    )

    assert seen == [
        [
            "bd", "update", "bd-1",
            "--append-notes", "only chance",
            "--add-label", "needs_human_review",
            "--status", "open",
        ]
    ]


def test_fail_task_always_releases_status_to_open() -> None:
    seen: list[list[str]] = []

    def run(argv, *, step):
        seen.append(argv)
        return _completed(argv)

    adapter = _make_adapter_with_run(run)
    # Try all three branches (first/within/exhausted).
    adapter.fail_task("bd-a", "r", retries_so_far=0, max_retries=3)
    adapter.fail_task("bd-b", "r", retries_so_far=2, max_retries=3)
    adapter.fail_task("bd-c", "r", retries_so_far=3, max_retries=3)

    for argv in seen:
        # --status open must be the last pair so release happens even
        # if earlier flags get reordered by a future refactor.
        assert argv[-2:] == ["--status", "open"]


# -----------------------------------------------------------------------
# close_task remains usable (carry-forward from transcription)
# -----------------------------------------------------------------------


def test_close_task_still_usable_for_swarm_success_path() -> None:
    seen: list[list[str]] = []

    def run(argv, *, step):
        seen.append(argv)
        return _completed(argv)

    adapter = _make_adapter_with_run(run)
    adapter.close_task("bd-42")
    # Same argv the transcription pipeline already pins; the swarm
    # orchestrator calls close_task at the end of the success path.
    assert seen == [["bd", "close", "bd-42"]]
