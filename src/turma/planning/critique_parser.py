"""Strict parser for critic output in the planning loop.

Pure module: no LangGraph, no backend calls, no filesystem I/O. Takes raw
critique text and returns a typed parse result that downstream phases
(round runner, state machine) consume to decide routing.

Status is authoritative for routing. Per-finding labels are detail, not
route inputs. Missing or malformed required elements produce a
ParseFailure that routes to needs_human_review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class CritiqueStatus(str, Enum):
    """Status token declared on the `## Status:` line."""

    BLOCKING = "blocking"
    NITS_ONLY = "nits_only"
    APPROVED = "approved"


class RouteDecision(str, Enum):
    """Next-state decision produced by the parser."""

    NEEDS_REVISION = "needs_revision"
    AWAITING_HUMAN_APPROVAL = "awaiting_human_approval"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class FindingKind(str, Enum):
    """Kind of a single critique finding, derived from the ID prefix."""

    BLOCKING = "blocking"
    NITS = "nits"
    QUESTION = "question"


@dataclass(frozen=True)
class Finding:
    """A single parsed critique finding."""

    id: str
    kind: FindingKind
    file_path: str
    message: str


@dataclass(frozen=True)
class ParsedCritique:
    """Successfully parsed critique."""

    status: CritiqueStatus
    findings: tuple[Finding, ...]
    route: RouteDecision


@dataclass(frozen=True)
class ParseFailure:
    """Critique could not be parsed; escalates to human review."""

    reason: str
    route: RouteDecision = RouteDecision.NEEDS_HUMAN_REVIEW


ParseResult = ParsedCritique | ParseFailure


_STATUS_TO_ROUTE: dict[CritiqueStatus, RouteDecision] = {
    CritiqueStatus.BLOCKING: RouteDecision.NEEDS_REVISION,
    CritiqueStatus.NITS_ONLY: RouteDecision.AWAITING_HUMAN_APPROVAL,
    CritiqueStatus.APPROVED: RouteDecision.AWAITING_HUMAN_APPROVAL,
}

_ID_PREFIX_TO_KIND: dict[str, FindingKind] = {
    "B": FindingKind.BLOCKING,
    "N": FindingKind.NITS,
    "Q": FindingKind.QUESTION,
}

_STATUS_LINE = re.compile(r"^##\s+Status:\s*(\S+)\s*$", re.MULTILINE)
_FINDING_LINE = re.compile(
    r"^-\s+"
    r"\[(?P<id>[^\]]*)\]\s+"
    r"\[(?P<label>[^\]]+)\]\s+"
    r"\[(?P<file>[^\]]+)\]\s+"
    r"(?P<msg>.+?)\s*$"
)
_FINDING_ID_TOKEN = re.compile(r"^[BNQ]\d+$")


def parse_critique(text: str) -> ParseResult:
    """Parse critique text into a typed result.

    The Status line alone decides routing. Finding bodies are collected
    for audit and loop-detection use by later phases.
    """
    status = _parse_status(text)
    if status is None:
        return ParseFailure(reason="missing or invalid Status line")

    findings_result = _parse_findings(text.splitlines())
    if isinstance(findings_result, ParseFailure):
        return findings_result

    return ParsedCritique(
        status=status,
        findings=tuple(findings_result),
        route=_STATUS_TO_ROUTE[status],
    )


def _parse_status(text: str) -> CritiqueStatus | None:
    match = _STATUS_LINE.search(text)
    if match is None:
        return None
    try:
        return CritiqueStatus(match.group(1))
    except ValueError:
        return None


def _parse_findings(lines: list[str]) -> list[Finding] | ParseFailure:
    findings: list[Finding] = []
    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.lstrip()
        if not stripped.startswith("- ["):
            continue

        match = _FINDING_LINE.match(line)
        if match is None:
            return ParseFailure(
                reason=f"malformed finding line: {stripped!r}"
            )

        id_token = match.group("id")
        if not _FINDING_ID_TOKEN.fullmatch(id_token):
            return ParseFailure(
                reason=f"malformed finding ID {id_token!r}"
            )

        findings.append(
            Finding(
                id=id_token,
                kind=_ID_PREFIX_TO_KIND[id_token[0]],
                file_path=match.group("file"),
                message=match.group("msg"),
            )
        )

    return findings
