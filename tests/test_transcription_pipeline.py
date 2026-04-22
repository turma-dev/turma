"""Integration tests for the transcription pipeline."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from turma.errors import PlanningError
from turma.transcription import (
    TranscriptionResult,
    transcribe_to_beads,
)
from turma.transcription.beads import BeadsTaskRef


# ---------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------


SAMPLE_TASKS_MD = textwrap.dedent(
    """
    ## Tasks

    ### 1. Extract primitives
    - [ ] Split the module
    - [ ] Add an injection seam

    ### 2. Write tests
    - [ ] Cover happy paths

    ### 3. Update the README
    - [ ] Document the feature

    ### 4. Draft the specification
    - [ ] Author the design
    """
).strip() + "\n"


def _dedent(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _setup_approved_change(
    tmp_path: Path,
    feature: str = "oauth",
    tasks_md: str | None = None,
) -> Path:
    """Create an `openspec/changes/<feature>/` dir with tasks.md + APPROVED."""
    change_dir = tmp_path / "openspec" / "changes" / feature
    change_dir.mkdir(parents=True)
    (change_dir / "tasks.md").write_text(tasks_md or SAMPLE_TASKS_MD)
    (change_dir / "APPROVED").write_text("approved\n")
    return change_dir


class StubBeadsAdapter:
    """A concrete stub that records adapter calls for assertion."""

    def __init__(self, *, list_result: tuple[BeadsTaskRef, ...] = ()) -> None:
        self.created_calls: list[dict] = []
        self.closed_ids: list[str] = []
        self._list_result = list_result
        self._next = 1
        # Optional failure injection: if set, create_task raises on the
        # Nth call (1-indexed).
        self.fail_on_create_number: int | None = None

    def create_task(
        self,
        *,
        title: str,
        description: str,
        bd_type: str,
        priority: int,
        feature: str,
        extra_labels: tuple[str, ...] = (),
        blocker_ids: tuple[str, ...] = (),
    ) -> str:
        call = {
            "title": title,
            "description": description,
            "bd_type": bd_type,
            "priority": priority,
            "feature": feature,
            "extra_labels": extra_labels,
            "blocker_ids": blocker_ids,
        }
        self.created_calls.append(call)
        if self.fail_on_create_number == len(self.created_calls):
            raise PlanningError(
                f"simulated adapter failure on create #{len(self.created_calls)}"
            )
        task_id = f"bd-{self._next}"
        self._next += 1
        call["returned_id"] = task_id
        return task_id

    def close_task(self, task_id: str) -> None:
        self.closed_ids.append(task_id)

    def list_feature_tasks(self, feature: str) -> tuple[BeadsTaskRef, ...]:
        return self._list_result


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------


def test_transcribe_happy_path_writes_marker_and_creates_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_dir = _setup_approved_change(tmp_path, feature="oauth")
    monkeypatch.chdir(tmp_path)

    adapter = StubBeadsAdapter()
    result = transcribe_to_beads("oauth", adapter)

    assert isinstance(result, TranscriptionResult)
    assert result.feature == "oauth"
    assert result.ids_by_section == {1: "bd-1", 2: "bd-2", 3: "bd-3", 4: "bd-4"}
    assert result.transcribed_path == change_dir / "TRANSCRIBED.md"

    titles = [c["title"] for c in adapter.created_calls]
    assert titles == [
        "Extract primitives",
        "Write tests",
        "Update the README",
        "Draft the specification",
    ]

    # Feature is present on every call; marker file exists.
    assert all(c["feature"] == "oauth" for c in adapter.created_calls)
    assert (change_dir / "TRANSCRIBED.md").exists()


def test_transcribe_translates_parser_types_to_bd_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_approved_change(tmp_path, feature="oauth")
    monkeypatch.chdir(tmp_path)

    adapter = StubBeadsAdapter()
    transcribe_to_beads("oauth", adapter)

    bd_types = [c["bd_type"] for c in adapter.created_calls]
    # 1: impl → task; 2: test → task; 3: docs → chore; 4: spec → decision.
    assert bd_types == ["task", "task", "chore", "decision"]


def test_transcribe_translates_priority_to_bd_scale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_md = _dedent(
        """
        ## Tasks

        ### 1. First
        - [ ] a

        ### 2. Second
        - [ ] b

        ### 3. Third
        - [ ] c

        ### 4. Fourth
        - [ ] d

        ### 5. Fifth
        - [ ] e

        ### 6. Sixth collapses to P4
        - [ ] f

        ### 7. Seventh also collapses
        - [ ] g
        """
    )
    _setup_approved_change(tmp_path, tasks_md=tasks_md)
    monkeypatch.chdir(tmp_path)

    adapter = StubBeadsAdapter()
    transcribe_to_beads("oauth", adapter)

    priorities = [c["priority"] for c in adapter.created_calls]
    assert priorities == [0, 1, 2, 3, 4, 4, 4]


def test_transcribe_composes_description_from_subtask_bullets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_approved_change(tmp_path)
    monkeypatch.chdir(tmp_path)

    adapter = StubBeadsAdapter()
    transcribe_to_beads("oauth", adapter)

    first_desc = adapter.created_calls[0]["description"]
    assert first_desc == (
        "- [ ] Split the module\n"
        "- [ ] Add an injection seam"
    )


def test_transcribe_resolves_blocker_ids_from_prior_create_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_approved_change(tmp_path, feature="oauth")
    monkeypatch.chdir(tmp_path)

    adapter = StubBeadsAdapter()
    transcribe_to_beads("oauth", adapter)

    # Default dependency chain: N blocked-by N-1. Section 1 has no
    # blockers; sections 2-4 each depend on the previously-created
    # task id.
    blocker_lists = [c["blocker_ids"] for c in adapter.created_calls]
    assert blocker_lists == [
        (),
        ("bd-1",),
        ("bd-2",),
        ("bd-3",),
    ]


def test_transcribe_emits_turma_type_extra_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_approved_change(tmp_path)
    monkeypatch.chdir(tmp_path)

    adapter = StubBeadsAdapter()
    transcribe_to_beads("oauth", adapter)

    # Every call carries the turma-type:<parser_type> label for
    # downstream filtering even though multiple parser types collapse
    # to a single bd_type.
    expected = [
        ("turma-type:impl",),
        ("turma-type:test",),
        ("turma-type:docs",),
        ("turma-type:spec",),
    ]
    assert [c["extra_labels"] for c in adapter.created_calls] == expected


def test_transcribed_md_schema_records_feature_and_ids_in_section_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_dir = _setup_approved_change(tmp_path, feature="oauth")
    monkeypatch.chdir(tmp_path)

    transcribe_to_beads("oauth", StubBeadsAdapter())

    text = (change_dir / "TRANSCRIBED.md").read_text()
    assert text.startswith("# TRANSCRIBED\n")
    assert "- feature: oauth" in text
    assert "- timestamp: " in text
    assert "- task_ids:" in text
    assert "  - section 1: bd-1" in text
    assert "  - section 4: bd-4" in text


# ---------------------------------------------------------------------
# Preflight / gating
# ---------------------------------------------------------------------


def test_transcribe_refuses_when_change_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PlanningError, match="does not exist"):
        transcribe_to_beads("no-such-feature", StubBeadsAdapter())


def test_transcribe_refuses_when_approved_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_dir = tmp_path / "openspec" / "changes" / "oauth"
    change_dir.mkdir(parents=True)
    (change_dir / "tasks.md").write_text(SAMPLE_TASKS_MD)
    # No APPROVED marker.
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PlanningError, match="requires the plan to be approved"):
        transcribe_to_beads("oauth", StubBeadsAdapter())


def test_transcribe_refuses_when_tasks_md_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_dir = tmp_path / "openspec" / "changes" / "oauth"
    change_dir.mkdir(parents=True)
    (change_dir / "APPROVED").write_text("approved\n")
    # No tasks.md.
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PlanningError, match="tasks.md not found"):
        transcribe_to_beads("oauth", StubBeadsAdapter())


def test_transcribe_refuses_when_already_transcribed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_dir = _setup_approved_change(tmp_path)
    (change_dir / "TRANSCRIBED.md").write_text(
        "# TRANSCRIBED\n\n- feature: oauth\n- task_ids:\n  - section 1: bd-prior\n"
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PlanningError, match="use --force to re-create"):
        transcribe_to_beads("oauth", StubBeadsAdapter())


def test_transcribe_refuses_on_orphan_feature_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_approved_change(tmp_path)
    monkeypatch.chdir(tmp_path)

    orphans = (
        BeadsTaskRef(id="bd-99", title="Left over", labels=("feature:oauth",)),
        BeadsTaskRef(id="bd-100", title="Also stale", labels=("feature:oauth",)),
    )
    adapter = StubBeadsAdapter(list_result=orphans)

    with pytest.raises(PlanningError) as exc:
        transcribe_to_beads("oauth", adapter)
    assert "feature-tagged tasks already exist" in str(exc.value)
    assert "bd-99" in str(exc.value)
    assert "bd-100" in str(exc.value)
    assert "--force" in str(exc.value)


def test_transcribe_surfaces_tasks_md_parse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = "### 1. Missing Tasks header\n- [ ] a\n"
    _setup_approved_change(tmp_path, tasks_md=malformed)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PlanningError, match="tasks.md parse failure"):
        transcribe_to_beads("oauth", StubBeadsAdapter())


# ---------------------------------------------------------------------
# --force teardown paths
# ---------------------------------------------------------------------


def test_force_with_transcribed_md_closes_recorded_ids_in_reverse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_dir = _setup_approved_change(tmp_path, feature="oauth")
    (change_dir / "TRANSCRIBED.md").write_text(
        _dedent(
            """
            # TRANSCRIBED

            - feature: oauth
            - timestamp: 2026-04-21T00:00:00+00:00
            - task_ids:
              - section 1: bd-prev-1
              - section 2: bd-prev-2
              - section 3: bd-prev-3
              - section 4: bd-prev-4
            """
        )
    )
    monkeypatch.chdir(tmp_path)

    adapter = StubBeadsAdapter()
    result = transcribe_to_beads("oauth", adapter, force=True)

    # Teardown closed the prior IDs in reverse section order.
    assert adapter.closed_ids == [
        "bd-prev-4", "bd-prev-3", "bd-prev-2", "bd-prev-1",
    ]
    # Then re-created fresh.
    assert [c["title"] for c in adapter.created_calls] == [
        "Extract primitives",
        "Write tests",
        "Update the README",
        "Draft the specification",
    ]
    assert result.ids_by_section == {1: "bd-1", 2: "bd-2", 3: "bd-3", 4: "bd-4"}
    assert (change_dir / "TRANSCRIBED.md").exists()


def test_force_refuses_empty_transcribed_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty TRANSCRIBED.md under --force must NOT proceed silently.

    If the marker existed but contains no parseable `- section N: <id>`
    lines, the pipeline cannot know which Beads tasks to tear down.
    Silently unlinking the marker and re-running would duplicate any
    already-existing feature-tagged tasks. Hard fail with an actionable
    message instead.
    """
    change_dir = _setup_approved_change(tmp_path)
    (change_dir / "TRANSCRIBED.md").write_text("")
    monkeypatch.chdir(tmp_path)

    adapter = StubBeadsAdapter()
    with pytest.raises(PlanningError, match="no `- section N: <id>` lines"):
        transcribe_to_beads("oauth", adapter, force=True)

    # Marker must remain on disk so the operator can inspect it.
    assert (change_dir / "TRANSCRIBED.md").exists()
    assert adapter.closed_ids == []
    assert adapter.created_calls == []


def test_force_refuses_malformed_transcribed_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker with no parseable id lines under --force also hard-fails."""
    change_dir = _setup_approved_change(tmp_path)
    (change_dir / "TRANSCRIBED.md").write_text(
        "# TRANSCRIBED\n\n"
        "- feature: oauth\n"
        "- timestamp: 2026-04-22T00:00:00+00:00\n"
        "- task_ids:\n"
        "  (list got corrupted somehow)\n"
    )
    monkeypatch.chdir(tmp_path)

    adapter = StubBeadsAdapter()
    with pytest.raises(PlanningError, match="Cannot determine what to tear down"):
        transcribe_to_beads("oauth", adapter, force=True)

    assert (change_dir / "TRANSCRIBED.md").exists()
    assert adapter.closed_ids == []
    assert adapter.created_calls == []


def test_force_with_orphans_closes_them_then_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_approved_change(tmp_path)
    monkeypatch.chdir(tmp_path)

    orphans = (
        BeadsTaskRef(id="bd-99", title="Stale", labels=("feature:oauth",)),
        BeadsTaskRef(id="bd-100", title="Also stale", labels=("feature:oauth",)),
    )
    adapter = StubBeadsAdapter(list_result=orphans)
    result = transcribe_to_beads("oauth", adapter, force=True)

    assert adapter.closed_ids == ["bd-99", "bd-100"]
    assert result.ids_by_section == {1: "bd-1", 2: "bd-2", 3: "bd-3", 4: "bd-4"}


def test_force_with_neither_marker_nor_orphans_is_a_clean_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_dir = _setup_approved_change(tmp_path)
    monkeypatch.chdir(tmp_path)

    adapter = StubBeadsAdapter()
    result = transcribe_to_beads("oauth", adapter, force=True)

    assert adapter.closed_ids == []
    assert result.ids_by_section == {1: "bd-1", 2: "bd-2", 3: "bd-3", 4: "bd-4"}
    assert (change_dir / "TRANSCRIBED.md").exists()


# ---------------------------------------------------------------------
# Partial failure
# ---------------------------------------------------------------------


def test_partial_failure_leaves_orphans_and_does_not_write_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change_dir = _setup_approved_change(tmp_path)
    monkeypatch.chdir(tmp_path)

    adapter = StubBeadsAdapter()
    adapter.fail_on_create_number = 3  # 2 succeed, 3rd raises

    with pytest.raises(PlanningError, match="simulated adapter failure"):
        transcribe_to_beads("oauth", adapter)

    # 2 tasks actually got created; 3rd raised; 4th never attempted.
    assert len(adapter.created_calls) == 3
    returned_ids = [
        c.get("returned_id") for c in adapter.created_calls if "returned_id" in c
    ]
    assert returned_ids == ["bd-1", "bd-2"]
    # Marker NOT written on failure.
    assert not (change_dir / "TRANSCRIBED.md").exists()
    # No close_task calls on the adapter (no automated rollback).
    assert adapter.closed_ids == []
