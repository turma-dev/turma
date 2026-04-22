"""Pure parser for the `tasks.md` artifact emitted by the planning loop.

Pure module: no subprocess, no filesystem, no network. Takes raw
`tasks.md` text and returns a typed parse result that downstream phases
(the Beads adapter and the translation pipeline) consume.

Grammar matches the critic-loop tasks.md shape:

```
## Tasks

### 1. Section title
- [ ] Subtask line one
- [ ] Subtask line two

### 2. Another section title
- [ ] Subtask
```

Optional inline markers on the section heading (space-separated, each in
square brackets):

- `[type: impl | test | docs | spec]` — explicit task type.
- `[blocked-by: N]` or `[blocked-by: N, M]` — explicit dependencies.

Defaults when markers are absent: type is inferred from title keywords
(`test` / `tests` → `test`; `doc` / `docs` / `readme` → `docs`; `spec` /
`specification` → `spec`; otherwise `impl`). Dependency defaults to the
previous section (`N` depends on `N-1`). Section 1 has no default
dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class TaskType(str, Enum):
    """The four Beads task types produced by transcription."""

    IMPL = "impl"
    TEST = "test"
    DOCS = "docs"
    SPEC = "spec"


@dataclass(frozen=True)
class ParsedTaskSection:
    """A single numbered section parsed from `tasks.md`."""

    number: int
    title: str
    task_type: TaskType
    priority: int
    blocked_by: tuple[int, ...]
    subtasks: tuple[str, ...]


@dataclass(frozen=True)
class ParsedTasks:
    """Successfully parsed `tasks.md`."""

    sections: tuple[ParsedTaskSection, ...]


@dataclass(frozen=True)
class TasksParseFailure:
    """`tasks.md` could not be parsed; transcription must refuse."""

    reason: str


ParseResult = ParsedTasks | TasksParseFailure


_TASKS_HEADER = re.compile(r"^##\s+Tasks\s*$")
_SECTION_HEADING = re.compile(
    r"^###\s+(?P<num>\d+)\.\s+(?P<rest>.+?)\s*$"
)
_SUBTASK_BULLET = re.compile(r"^-\s+\[\s*\]\s*(?P<body>.*)$")
_TYPE_MARKER = re.compile(r"\[type:\s*(?P<token>[^\]]*?)\s*\]")
_BLOCKED_BY_MARKER = re.compile(r"\[blocked-by:\s*(?P<body>[^\]]*?)\s*\]")

_TYPE_KEYWORD_PATTERNS: tuple[tuple[str, TaskType], ...] = (
    # Order matters: first match wins. Matches the precedence listed in
    # the v1 design doc.
    ("test", TaskType.TEST),     # also matches "tests" / "testing"
    ("doc", TaskType.DOCS),      # also matches "docs" / "documentation"
    ("readme", TaskType.DOCS),
    ("spec", TaskType.SPEC),     # also matches "specification"
)


def parse_tasks_md(text: str) -> ParseResult:
    """Parse a `tasks.md` string into a typed result."""
    lines = text.splitlines()

    tasks_idx = _find_tasks_header(lines)
    if tasks_idx is None:
        return TasksParseFailure(reason="missing `## Tasks` header")

    raw_sections = _extract_raw_sections(lines[tasks_idx + 1:])
    if not raw_sections:
        return TasksParseFailure(
            reason="no sections found after `## Tasks` header"
        )

    for expected, raw in enumerate(raw_sections, start=1):
        if raw[0] != expected:
            return TasksParseFailure(
                reason=(
                    "section numbers must be ascending from 1; expected "
                    f"section {expected} but found section {raw[0]}"
                )
            )

    parsed: list[ParsedTaskSection] = []
    for raw in raw_sections:
        section_or_failure = _parse_section(
            num=raw[0],
            heading_rest=raw[1],
            body_lines=raw[2],
            total_sections=len(raw_sections),
        )
        if isinstance(section_or_failure, TasksParseFailure):
            return section_or_failure
        parsed.append(section_or_failure)

    return ParsedTasks(sections=tuple(parsed))


def _find_tasks_header(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if _TASKS_HEADER.match(line):
            return i
    return None


def _extract_raw_sections(
    lines: list[str],
) -> list[tuple[int, str, list[str]]]:
    """Return a list of (number, heading_rest, body_lines) tuples.

    Lines appearing before the first section heading are ignored; lines
    appearing between sections are attributed to the preceding section's
    body (subtask extraction will filter them).
    """
    sections: list[tuple[int, str, list[str]]] = []
    current: tuple[int, str] | None = None
    current_body: list[str] = []
    for line in lines:
        match = _SECTION_HEADING.match(line)
        if match:
            if current is not None:
                sections.append((current[0], current[1], current_body))
            current = (int(match.group("num")), match.group("rest"))
            current_body = []
        elif current is not None:
            current_body.append(line)
    if current is not None:
        sections.append((current[0], current[1], current_body))
    return sections


def _parse_section(
    *,
    num: int,
    heading_rest: str,
    body_lines: list[str],
    total_sections: int,
) -> ParsedTaskSection | TasksParseFailure:
    explicit_type = _extract_type_marker(num, heading_rest)
    if isinstance(explicit_type, TasksParseFailure):
        return explicit_type

    explicit_blocked_by = _extract_blocked_by_marker(
        num=num,
        heading_rest=heading_rest,
        total_sections=total_sections,
    )
    if isinstance(explicit_blocked_by, TasksParseFailure):
        return explicit_blocked_by

    title = _strip_markers(heading_rest).strip()
    if not title:
        return TasksParseFailure(
            reason=f"section {num}: title is empty after stripping markers"
        )

    subtasks = _extract_subtasks(body_lines)
    if not subtasks:
        return TasksParseFailure(
            reason=f"section {num} has no `- [ ]` subtasks"
        )

    task_type = explicit_type if explicit_type is not None else _infer_type(title)

    if explicit_blocked_by is not None:
        blocked_by = explicit_blocked_by
    elif num == 1:
        blocked_by = ()
    else:
        blocked_by = (num - 1,)

    return ParsedTaskSection(
        number=num,
        title=title,
        task_type=task_type,
        priority=num,
        blocked_by=blocked_by,
        subtasks=tuple(subtasks),
    )


def _extract_type_marker(
    num: int,
    heading_rest: str,
) -> TaskType | None | TasksParseFailure:
    match = _TYPE_MARKER.search(heading_rest)
    if match is None:
        return None
    token = match.group("token").strip()
    try:
        return TaskType(token)
    except ValueError:
        return TasksParseFailure(
            reason=f"section {num}: unknown type token {token!r}"
        )


def _extract_blocked_by_marker(
    *,
    num: int,
    heading_rest: str,
    total_sections: int,
) -> tuple[int, ...] | None | TasksParseFailure:
    match = _BLOCKED_BY_MARKER.search(heading_rest)
    if match is None:
        return None

    raw = match.group("body").strip()
    if not raw:
        return TasksParseFailure(
            reason=f"section {num}: empty blocked-by marker"
        )

    refs: list[int] = []
    for part in (p.strip() for p in raw.split(",")):
        if not part.isdigit():
            return TasksParseFailure(
                reason=(
                    f"section {num}: blocked-by marker contains "
                    f"non-integer {part!r}"
                )
            )
        ref = int(part)
        if ref == num:
            return TasksParseFailure(
                reason=f"section {num}: self reference in blocked-by"
            )
        if ref > num:
            return TasksParseFailure(
                reason=(
                    f"section {num}: forward reference to section {ref} "
                    "in blocked-by"
                )
            )
        if ref < 1 or ref > total_sections:
            return TasksParseFailure(
                reason=(
                    f"section {num}: blocked-by references non-existent "
                    f"section {ref}"
                )
            )
        refs.append(ref)

    return tuple(refs)


def _strip_markers(heading_rest: str) -> str:
    cleaned = _TYPE_MARKER.sub("", heading_rest)
    cleaned = _BLOCKED_BY_MARKER.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_subtasks(body_lines: list[str]) -> list[str]:
    """Extract subtask bodies preserving continuation-line formatting.

    A subtask starts at a `- [ ]` bullet line and extends until the next
    bullet, a non-indented non-empty line (prose), or end-of-section.
    Internal blank lines and indented continuation lines are preserved
    verbatim in the captured body; leading `- [ ] ` is stripped.
    """
    subtasks: list[str] = []
    current: list[str] | None = None

    for line in body_lines:
        match = _SUBTASK_BULLET.match(line)
        if match:
            if current is not None:
                subtasks.append(_finalize_subtask(current))
            current = [match.group("body")]
            continue

        if current is None:
            # Prose before the first bullet — ignored.
            continue

        if line.strip() == "":
            # Internal blank line: could be part of the subtask or a
            # separator. Keep it for now; if no continuation follows,
            # _finalize_subtask trims trailing blanks.
            current.append(line)
        elif line.startswith((" ", "\t")):
            current.append(line)
        else:
            # Unindented prose ends the current subtask and is dropped.
            subtasks.append(_finalize_subtask(current))
            current = None

    if current is not None:
        subtasks.append(_finalize_subtask(current))

    return subtasks


def _finalize_subtask(lines: list[str]) -> str:
    trimmed = list(lines)
    while trimmed and trimmed[-1].strip() == "":
        trimmed.pop()
    return "\n".join(trimmed)


def _infer_type(title: str) -> TaskType:
    lowered = title.lower()
    for keyword, task_type in _TYPE_KEYWORD_PATTERNS:
        if keyword in lowered:
            return task_type
    return TaskType.IMPL
