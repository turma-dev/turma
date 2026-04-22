"""Translate approved planning artifacts into a feature-tagged Beads task set.

Entry point: `transcribe_to_beads(feature, adapter, *, force=False)`.

Pipeline order (per `openspec/changes/beads-transcription/design.md`):

1. Verify the change directory exists.
2. Gate on the `APPROVED` terminal marker (authoritative per the
   planning-terminal-state authority order).
3. Idempotency preflight:
   - If `TRANSCRIBED.md` is present → refuse unless `force=True`.
   - Else query `adapter.list_feature_tasks(feature)` for orphans
     from a prior failed attempt → refuse unless `force=True`.
4. `--force` teardown: close the recorded IDs (marker present) or
   the orphan IDs (marker absent) before re-running. `--force` with
   neither is a no-op.
5. Parse `tasks.md` via the pure parser; parse failures surface as
   `PlanningError` with the parser's reason.
6. Per section in ascending order:
   a. Translate parser task_type → bd-native type
      (`impl`/`test` → `task`, `docs` → `chore`, `spec` → `decision`).
   b. Translate parser priority → bd priority (`min(N-1, 4)`).
   c. Compose the Beads description from the verbatim subtask bullets.
   d. Resolve `blocker_ids` from section numbers to `bd` task ids
      recorded earlier in this run.
   e. Invoke `adapter.create_task(...)`; record the returned id.
7. On full success, write `TRANSCRIBED.md` in the change dir with
   the feature name, timestamp, and created task ids in section order.
8. Partial failure leaves feature-tagged orphans on the Beads side;
   `TRANSCRIBED.md` is NOT written. Recovery is manual `bd close` or
   a `--force` retry using the orphan-teardown path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from turma.errors import PlanningError
from turma.transcription.beads import BeadsAdapter, BeadsTaskRef
from turma.transcription.tasks_md import (
    ParsedTasks,
    ParsedTaskSection,
    TaskType,
    TasksParseFailure,
    parse_tasks_md,
)


@dataclass(frozen=True)
class TranscriptionResult:
    """Successful-transcription summary."""

    feature: str
    ids_by_section: dict[int, str]
    transcribed_path: Path


_PARSER_TO_BD_TYPE: dict[TaskType, str] = {
    TaskType.IMPL: "task",
    TaskType.TEST: "task",
    TaskType.DOCS: "chore",
    TaskType.SPEC: "decision",
}

_MAX_BD_PRIORITY = 4  # bd's priority scale is 0-4; 0 is highest.

_TRANSCRIBED_ID_LINE = re.compile(
    r"^\s*-\s+section\s+(?P<num>\d+)\s*:\s*(?P<id>\S+)\s*$",
    re.MULTILINE,
)


def transcribe_to_beads(
    feature: str,
    adapter: BeadsAdapter,
    *,
    force: bool = False,
) -> TranscriptionResult:
    """Translate an approved plan into a feature-tagged Beads task set."""
    change_dir = Path.cwd() / "openspec" / "changes" / feature
    if not change_dir.exists():
        raise PlanningError(
            f"openspec/changes/{feature}/ does not exist"
        )

    if not (change_dir / "APPROVED").exists():
        raise PlanningError(
            "plan-to-beads requires the plan to be approved first; "
            f"no APPROVED marker in openspec/changes/{feature}/"
        )

    transcribed_path = change_dir / "TRANSCRIBED.md"
    _run_idempotency_preflight(
        feature=feature,
        transcribed_path=transcribed_path,
        adapter=adapter,
        force=force,
    )

    tasks_path = change_dir / "tasks.md"
    if not tasks_path.exists():
        raise PlanningError(
            f"tasks.md not found in openspec/changes/{feature}/"
        )
    parse_result = parse_tasks_md(tasks_path.read_text())
    if isinstance(parse_result, TasksParseFailure):
        raise PlanningError(
            f"tasks.md parse failure: {parse_result.reason}"
        )
    assert isinstance(parse_result, ParsedTasks)

    ids_by_section: dict[int, str] = {}
    for section in parse_result.sections:
        new_id = _create_section_task(
            section=section,
            feature=feature,
            adapter=adapter,
            ids_by_section=ids_by_section,
        )
        ids_by_section[section.number] = new_id

    _write_transcribed_md(transcribed_path, feature, ids_by_section)

    return TranscriptionResult(
        feature=feature,
        ids_by_section=ids_by_section,
        transcribed_path=transcribed_path,
    )


def _run_idempotency_preflight(
    *,
    feature: str,
    transcribed_path: Path,
    adapter: BeadsAdapter,
    force: bool,
) -> None:
    """Decide what to do about prior-attempt state before running the pipeline."""
    if transcribed_path.exists():
        if not force:
            raise PlanningError(
                "change already transcribed to Beads; use --force to re-create"
            )
        recorded_ids = _read_transcribed_ids(transcribed_path)
        if not recorded_ids:
            # A marker we cannot parse is untrustworthy — silently
            # unlinking it here and creating fresh tasks would
            # duplicate any already-existing feature-tagged tasks.
            # Refuse and force the operator to resolve manually.
            raise PlanningError(
                f"TRANSCRIBED.md at {transcribed_path} exists but no "
                "`- section N: <id>` lines could be parsed. Cannot "
                "determine what to tear down. Inspect the file, delete "
                "it manually, or close feature-tagged tasks with "
                "`bd close` and retry."
            )
        for task_id in reversed(recorded_ids):
            adapter.close_task(task_id)
        transcribed_path.unlink()
        return

    orphans = adapter.list_feature_tasks(feature)
    if not orphans:
        return

    if not force:
        ids = ", ".join(orphan.id for orphan in orphans)
        raise PlanningError(
            "feature-tagged tasks already exist in Beads from a prior "
            f"failed transcription (ids: {ids}). Close them with "
            f"`bd close {' '.join(o.id for o in orphans)}` or retry "
            "with --force."
        )

    for orphan in orphans:
        adapter.close_task(orphan.id)


def _create_section_task(
    *,
    section: ParsedTaskSection,
    feature: str,
    adapter: BeadsAdapter,
    ids_by_section: dict[int, str],
) -> str:
    """Translate a parsed section into a single `adapter.create_task` call."""
    bd_type = _PARSER_TO_BD_TYPE[section.task_type]
    priority = min(section.number - 1, _MAX_BD_PRIORITY)
    description = _compose_description(section.subtasks)
    blocker_ids = tuple(
        ids_by_section[num] for num in section.blocked_by
    )
    extra_labels = (f"turma-type:{section.task_type.value}",)
    return adapter.create_task(
        title=section.title,
        description=description,
        bd_type=bd_type,
        priority=priority,
        feature=feature,
        extra_labels=extra_labels,
        blocker_ids=blocker_ids,
    )


def _compose_description(subtasks: tuple[str, ...]) -> str:
    """Reconstruct the tasks.md subtask list as markdown for the Beads body."""
    return "\n".join(f"- [ ] {subtask}" for subtask in subtasks)


def _read_transcribed_ids(path: Path) -> list[str]:
    """Return the Beads task ids recorded in TRANSCRIBED.md, in section order."""
    text = path.read_text()
    pairs = sorted(
        (int(m.group("num")), m.group("id"))
        for m in _TRANSCRIBED_ID_LINE.finditer(text)
    )
    return [task_id for _, task_id in pairs]


def _write_transcribed_md(
    path: Path,
    feature: str,
    ids_by_section: dict[int, str],
) -> None:
    """Write the TRANSCRIBED.md marker with the created task ids in section order."""
    lines = [
        "# TRANSCRIBED",
        "",
        f"- feature: {feature}",
        f"- timestamp: {datetime.now(UTC).isoformat()}",
        "- task_ids:",
    ]
    for num in sorted(ids_by_section):
        lines.append(f"  - section {num}: {ids_by_section[num]}")
    path.write_text("\n".join(lines) + "\n")
