"""Tests for the critic output parser."""

import textwrap

import pytest

from turma.planning.critique_parser import (
    CritiqueStatus,
    Finding,
    FindingKind,
    ParseFailure,
    ParsedCritique,
    RouteDecision,
    parse_critique,
)


def _dedent(text: str) -> str:
    return textwrap.dedent(text).strip() + "\n"


def test_blocking_status_routes_to_needs_revision():
    result = parse_critique(_dedent("""
        ## Status: blocking

        ## Findings
        - [B001] [blocking] [design.md] Retry budget undefined for spec task type
    """))

    assert isinstance(result, ParsedCritique)
    assert result.status is CritiqueStatus.BLOCKING
    assert result.route is RouteDecision.NEEDS_REVISION


def test_nits_only_status_routes_to_awaiting_human_approval():
    result = parse_critique(_dedent("""
        ## Status: nits_only

        ## Findings
        - [N001] [nits] [tasks.md] Task could be split
    """))

    assert isinstance(result, ParsedCritique)
    assert result.status is CritiqueStatus.NITS_ONLY
    assert result.route is RouteDecision.AWAITING_HUMAN_APPROVAL


def test_approved_status_routes_to_awaiting_human_approval():
    result = parse_critique(_dedent("""
        ## Status: approved

        ## Findings
    """))

    assert isinstance(result, ParsedCritique)
    assert result.status is CritiqueStatus.APPROVED
    assert result.route is RouteDecision.AWAITING_HUMAN_APPROVAL
    assert result.findings == ()


def test_approved_without_findings_section_is_valid():
    result = parse_critique("## Status: approved\n")

    assert isinstance(result, ParsedCritique)
    assert result.status is CritiqueStatus.APPROVED
    assert result.findings == ()


def test_missing_status_line_escalates_to_needs_human_review():
    result = parse_critique(_dedent("""
        ## Findings
        - [B001] [blocking] [design.md] Something
    """))

    assert isinstance(result, ParseFailure)
    assert result.route is RouteDecision.NEEDS_HUMAN_REVIEW


def test_invalid_status_token_escalates_to_needs_human_review():
    result = parse_critique(_dedent("""
        ## Status: looks_good

        ## Findings
    """))

    assert isinstance(result, ParseFailure)
    assert result.route is RouteDecision.NEEDS_HUMAN_REVIEW


def test_capitalized_status_token_is_rejected():
    result = parse_critique(_dedent("""
        ## Status: Blocking

        ## Findings
        - [B001] [blocking] [design.md] Retry budget undefined
    """))

    assert isinstance(result, ParseFailure)
    assert result.route is RouteDecision.NEEDS_HUMAN_REVIEW


def test_empty_critique_escalates_to_needs_human_review():
    result = parse_critique("")

    assert isinstance(result, ParseFailure)
    assert result.route is RouteDecision.NEEDS_HUMAN_REVIEW


def test_finding_kinds_are_derived_from_id_prefix():
    result = parse_critique(_dedent("""
        ## Status: blocking

        ## Findings
        - [B001] [blocking] [design.md] Blocking finding
        - [N003] [nits] [tasks.md] Nit finding
        - [Q002] [question] [proposal.md] Question finding
    """))

    assert isinstance(result, ParsedCritique)
    kinds = [f.kind for f in result.findings]
    assert kinds == [FindingKind.BLOCKING, FindingKind.NITS, FindingKind.QUESTION]


def test_findings_preserve_declaration_order():
    result = parse_critique(_dedent("""
        ## Status: blocking

        ## Findings
        - [B002] [blocking] [design.md] Second blocking
        - [B001] [blocking] [design.md] First blocking
        - [N005] [nits] [tasks.md] A nit
    """))

    assert isinstance(result, ParsedCritique)
    ids = [f.id for f in result.findings]
    assert ids == ["B002", "B001", "N005"]


def test_malformed_finding_id_escalates_to_needs_human_review():
    result = parse_critique(_dedent("""
        ## Status: blocking

        ## Findings
        - [Xfoo] [blocking] [design.md] Bad prefix
    """))

    assert isinstance(result, ParseFailure)
    assert result.route is RouteDecision.NEEDS_HUMAN_REVIEW


def test_missing_finding_id_escalates_to_needs_human_review():
    result = parse_critique(_dedent("""
        ## Status: blocking

        ## Findings
        - [blocking] [design.md] ID omitted
    """))

    assert isinstance(result, ParseFailure)
    assert result.route is RouteDecision.NEEDS_HUMAN_REVIEW


def test_empty_finding_id_brackets_escalate_to_needs_human_review():
    result = parse_critique(_dedent("""
        ## Status: blocking

        ## Findings
        - [] [blocking] [design.md] No ID
    """))

    assert isinstance(result, ParseFailure)
    assert result.route is RouteDecision.NEEDS_HUMAN_REVIEW


def test_file_path_with_line_number_is_preserved_verbatim():
    result = parse_critique(_dedent("""
        ## Status: blocking

        ## Findings
        - [B001] [blocking] [design.md#L42] Retry budget undefined
    """))

    assert isinstance(result, ParsedCritique)
    assert result.findings[0].file_path == "design.md#L42"


def test_question_finding_under_blocking_does_not_override_route():
    result = parse_critique(_dedent("""
        ## Status: blocking

        ## Findings
        - [Q001] [question] [proposal.md] Does this cover refresh?
    """))

    assert isinstance(result, ParsedCritique)
    assert result.status is CritiqueStatus.BLOCKING
    assert result.route is RouteDecision.NEEDS_REVISION
    assert result.findings[0].kind is FindingKind.QUESTION


def test_question_finding_under_nits_only_does_not_override_route():
    result = parse_critique(_dedent("""
        ## Status: nits_only

        ## Findings
        - [Q001] [question] [proposal.md] Worth expanding later?
        - [N001] [nits] [tasks.md] Minor wording
    """))

    assert isinstance(result, ParsedCritique)
    assert result.status is CritiqueStatus.NITS_ONLY
    assert result.route is RouteDecision.AWAITING_HUMAN_APPROVAL


def test_parser_tolerates_prose_between_findings():
    result = parse_critique(_dedent("""
        ## Status: blocking

        ## Findings

        Some prose the critic added.

        - [B001] [blocking] [design.md] Real finding

        More prose.

        - [B002] [blocking] [tasks.md] Another real finding
    """))

    assert isinstance(result, ParsedCritique)
    assert [f.id for f in result.findings] == ["B001", "B002"]


def test_parser_is_pure_on_unusual_whitespace():
    # Tabs and extra spaces in the Status line are tolerated.
    result = parse_critique("## Status:    blocking   \n\n## Findings\n")

    assert isinstance(result, ParsedCritique)
    assert result.status is CritiqueStatus.BLOCKING


def test_finding_message_captures_remaining_text():
    result = parse_critique(_dedent("""
        ## Status: blocking

        ## Findings
        - [B001] [blocking] [design.md] Retry budget undefined for spec task type
    """))

    assert isinstance(result, ParsedCritique)
    assert result.findings[0].message == (
        "Retry budget undefined for spec task type"
    )


def test_parse_failure_carries_reason():
    result = parse_critique("no status here")

    assert isinstance(result, ParseFailure)
    assert result.reason
    assert result.route is RouteDecision.NEEDS_HUMAN_REVIEW


@pytest.mark.parametrize(
    "id_prefix,expected_kind",
    [
        ("B001", FindingKind.BLOCKING),
        ("N042", FindingKind.NITS),
        ("Q999", FindingKind.QUESTION),
    ],
)
def test_all_id_prefixes_map_to_expected_kinds(id_prefix, expected_kind):
    text = _dedent(f"""
        ## Status: nits_only

        ## Findings
        - [{id_prefix}] [nits] [design.md] A finding
    """)

    result = parse_critique(text)
    assert isinstance(result, ParsedCritique)
    assert result.findings[0].kind is expected_kind


def test_finding_dataclass_is_frozen():
    finding = Finding(
        id="B001",
        kind=FindingKind.BLOCKING,
        file_path="design.md",
        message="something",
    )
    with pytest.raises(Exception):
        finding.id = "B002"  # type: ignore[misc]
