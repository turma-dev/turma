import pytest

from turma.cli import _build_resume_request, _reject_stray_resume_flags, build_parser
from turma.errors import PlanningError
from turma.planning.resume import ResumeAction


def test_parser_builds() -> None:
    parser = build_parser()
    assert parser.prog == "turma"


def _parse_plan(*extra: str):
    parser = build_parser()
    return parser.parse_args(["plan", "--feature", "f", *extra])


def test_resume_status_when_no_action_flags() -> None:
    args = _parse_plan("--resume")
    assert _build_resume_request(args).action is ResumeAction.STATUS


def test_resume_approve_builds_approve_request() -> None:
    args = _parse_plan("--resume", "--approve")
    assert _build_resume_request(args).action is ResumeAction.APPROVE


def test_resume_revise_carries_reason() -> None:
    args = _parse_plan("--resume", "--revise", "too vague")
    request = _build_resume_request(args)
    assert request.action is ResumeAction.REVISE
    assert request.reason == "too vague"


def test_resume_approve_override_builds_override_request() -> None:
    args = _parse_plan("--resume", "--approve", "--override", "critic wrong")
    request = _build_resume_request(args)
    assert request.action is ResumeAction.OVERRIDE_APPROVE
    assert request.reason == "critic wrong"


def test_multiple_primary_flags_rejected() -> None:
    args = _parse_plan("--resume", "--approve", "--revise", "x")
    with pytest.raises(PlanningError, match="exactly one"):
        _build_resume_request(args)


def test_override_requires_approve() -> None:
    args = _parse_plan("--resume", "--override", "nope")
    with pytest.raises(PlanningError, match="must be combined with --approve"):
        _build_resume_request(args)


def test_override_cannot_combine_with_revise() -> None:
    args = _parse_plan(
        "--resume", "--approve", "--revise", "x", "--override", "y"
    )
    with pytest.raises(PlanningError):
        _build_resume_request(args)


def test_stray_resume_flags_rejected_without_resume() -> None:
    args = _parse_plan("--approve")
    with pytest.raises(PlanningError, match="require --resume"):
        _reject_stray_resume_flags(args)
