"""Tests for the tasks.md parser."""

import textwrap

import pytest

from turma.transcription.tasks_md import (
    ParsedTaskSection,
    ParsedTasks,
    TaskType,
    TasksParseFailure,
    parse_tasks_md,
)


def _dedent(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def test_parses_single_impl_section():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. Extract planner primitives
        - [ ] Split planning.py
        - [ ] Add injection seam
    """))

    assert isinstance(result, ParsedTasks)
    assert len(result.sections) == 1
    section = result.sections[0]
    assert section.number == 1
    assert section.title == "Extract planner primitives"
    assert section.task_type is TaskType.IMPL
    assert section.priority == 1
    assert section.blocked_by == ()
    assert section.subtasks == (
        "Split planning.py",
        "Add injection seam",
    )


def test_multiple_sections_use_default_sequential_dependencies():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. First
        - [ ] a

        ### 2. Second
        - [ ] b

        ### 3. Third
        - [ ] c
    """))

    assert isinstance(result, ParsedTasks)
    blocked_by = [s.blocked_by for s in result.sections]
    assert blocked_by == [(), (1,), (2,)]


def test_priority_equals_section_number():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. First
        - [ ] a

        ### 2. Second
        - [ ] b
    """))

    assert isinstance(result, ParsedTasks)
    assert [s.priority for s in result.sections] == [1, 2]


def test_type_inference_defaults_to_impl():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. Build the thing
        - [ ] a
    """))

    assert isinstance(result, ParsedTasks)
    assert result.sections[0].task_type is TaskType.IMPL


def test_type_inference_test_keyword():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. Write tests for the parser
        - [ ] a
    """))

    assert isinstance(result, ParsedTasks)
    assert result.sections[0].task_type is TaskType.TEST


def test_type_inference_docs_keyword():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. Update the README
        - [ ] a

        ### 2. Add docs for the API
        - [ ] b
    """))

    assert isinstance(result, ParsedTasks)
    assert result.sections[0].task_type is TaskType.DOCS
    assert result.sections[1].task_type is TaskType.DOCS


def test_type_inference_spec_keyword():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. Draft the specification
        - [ ] a
    """))

    assert isinstance(result, ParsedTasks)
    assert result.sections[0].task_type is TaskType.SPEC


def test_explicit_type_marker_overrides_inference():
    # Title contains "test" which would infer TEST, but marker says IMPL.
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. [type: impl] Write the test harness
        - [ ] a
    """))

    assert isinstance(result, ParsedTasks)
    assert result.sections[0].task_type is TaskType.IMPL
    # Title should have the marker stripped
    assert result.sections[0].title == "Write the test harness"


def test_explicit_type_marker_unknown_token_rejected():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. [type: weird] Do a thing
        - [ ] a
    """))

    assert isinstance(result, TasksParseFailure)
    assert "unknown type token" in result.reason


def test_explicit_blocked_by_single_overrides_default():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. First
        - [ ] a

        ### 2. Second
        - [ ] b

        ### 3. [blocked-by: 1] Third
        - [ ] c
    """))

    assert isinstance(result, ParsedTasks)
    # Section 3's default would be (2,); marker overrides to (1,).
    assert result.sections[2].blocked_by == (1,)


def test_explicit_blocked_by_multiple():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. First
        - [ ] a

        ### 2. Second
        - [ ] b

        ### 3. [blocked-by: 1, 2] Third
        - [ ] c
    """))

    assert isinstance(result, ParsedTasks)
    assert result.sections[2].blocked_by == (1, 2)


def test_self_reference_rejected():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. First
        - [ ] a

        ### 2. [blocked-by: 2] Second
        - [ ] b
    """))

    assert isinstance(result, TasksParseFailure)
    assert "self reference" in result.reason


def test_forward_reference_rejected():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. First
        - [ ] a

        ### 2. [blocked-by: 3] Second
        - [ ] b

        ### 3. Third
        - [ ] c
    """))

    assert isinstance(result, TasksParseFailure)
    assert "forward reference" in result.reason


def test_nonexistent_reference_rejected():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. First
        - [ ] a

        ### 2. [blocked-by: 99] Second
        - [ ] b
    """))

    assert isinstance(result, TasksParseFailure)
    # 99 is both forward and non-existent. Either reason is acceptable, but
    # the forward check fires first; both rules protect correctness.
    assert (
        "non-existent" in result.reason
        or "forward reference" in result.reason
    )


def test_empty_blocked_by_marker_rejected():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. First
        - [ ] a

        ### 2. [blocked-by: ] Second
        - [ ] b
    """))

    assert isinstance(result, TasksParseFailure)
    assert "empty" in result.reason.lower() and "blocked-by" in result.reason


def test_non_integer_blocked_by_rejected():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. First
        - [ ] a

        ### 2. [blocked-by: abc] Second
        - [ ] b
    """))

    assert isinstance(result, TasksParseFailure)
    assert "non-integer" in result.reason


def test_missing_tasks_header_rejected():
    result = parse_tasks_md(_dedent("""
        ### 1. A section without the header
        - [ ] a
    """))

    assert isinstance(result, TasksParseFailure)
    assert "## Tasks" in result.reason


def test_non_ascending_section_numbers_rejected():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. First
        - [ ] a

        ### 3. Third (gap)
        - [ ] c
    """))

    assert isinstance(result, TasksParseFailure)
    assert "ascending" in result.reason


def test_empty_section_rejected():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. Empty section

        ### 2. Filled section
        - [ ] b
    """))

    assert isinstance(result, TasksParseFailure)
    assert "no `- [ ]` subtasks" in result.reason


def test_no_sections_after_header_rejected():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        Some stray prose with no sections.
    """))

    assert isinstance(result, TasksParseFailure)
    assert "no sections" in result.reason


def test_subtasks_preserved_verbatim():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. With special content
        - [ ] Has `code` and "quotes" and a [link](url)
        - [ ] Plain second subtask
    """))

    assert isinstance(result, ParsedTasks)
    assert result.sections[0].subtasks == (
        'Has `code` and "quotes" and a [link](url)',
        "Plain second subtask",
    )


def test_subtask_continuation_lines_preserved():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. Multi-line subtasks
        - [ ] First line of subtask
              second line continues
              third line continues
        - [ ] Second subtask on its own
    """))

    assert isinstance(result, ParsedTasks)
    assert len(result.sections[0].subtasks) == 2
    first = result.sections[0].subtasks[0]
    assert "First line of subtask" in first
    assert "second line continues" in first
    assert "third line continues" in first
    assert result.sections[0].subtasks[1] == "Second subtask on its own"


def test_prose_between_sections_ignored():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        Some note about the tasks overall.

        ### 1. First
        - [ ] a

        Prose between sections.

        ### 2. Second
        - [ ] b
    """))

    assert isinstance(result, ParsedTasks)
    assert len(result.sections) == 2


def test_markers_stripped_from_title():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. [type: test] [blocked-by: ] Title with both markers
        - [ ] a
    """))

    # The empty blocked-by marker is rejected; the type marker alone should
    # leave the title clean. Here we exercise the empty-blocked-by rejection.
    assert isinstance(result, TasksParseFailure)


def test_title_cleaned_with_both_valid_markers():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. First
        - [ ] a

        ### 2. [type: test] [blocked-by: 1] Second with markers
        - [ ] b
    """))

    assert isinstance(result, ParsedTasks)
    s = result.sections[1]
    assert s.task_type is TaskType.TEST
    assert s.blocked_by == (1,)
    assert s.title == "Second with markers"


def test_section_1_explicit_blocked_by_rejects():
    # Section 1 cannot reference any prior section; explicit marker fires
    # the self-reference or forward-reference check.
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. [blocked-by: 1] First
        - [ ] a
    """))

    assert isinstance(result, TasksParseFailure)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Extract planner primitives", TaskType.IMPL),
        ("Write tests for the parser", TaskType.TEST),
        ("Update the README", TaskType.DOCS),
        ("Add docs for the API", TaskType.DOCS),
        ("Draft the specification", TaskType.SPEC),
        ("Write a spec", TaskType.SPEC),
    ],
)
def test_type_inference_table(title, expected):
    result = parse_tasks_md(_dedent(f"""
        ## Tasks

        ### 1. {title}
        - [ ] a
    """))

    assert isinstance(result, ParsedTasks)
    assert result.sections[0].task_type is expected


def test_parsed_task_section_is_frozen():
    result = parse_tasks_md(_dedent("""
        ## Tasks

        ### 1. A
        - [ ] a
    """))

    assert isinstance(result, ParsedTasks)
    section = result.sections[0]
    with pytest.raises(Exception):
        section.title = "mutated"  # type: ignore[misc]
