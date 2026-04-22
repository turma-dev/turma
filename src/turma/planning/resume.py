"""Resume dispatch for the planning critic loop.

Thin CLI-facing layer. Takes a structured resume action, constructs a
read-only planning session, and delegates to the state machine's resume
primitives. Filesystem markers are written as side effects of the
chosen action, not as triggers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from turma.errors import PlanningError
from turma.planning import PlanningServices, _prepare_planning_session
from turma.planning.state_machine import (
    PlanningGraphResult,
    override_needs_human_review,
    read_planning_state,
    resume_awaiting_human_approval,
)


class ResumeAction(str, Enum):
    """Top-level resume commands available from the CLI."""

    STATUS = "status"
    APPROVE = "approve"
    REVISE = "revise"
    ABANDON = "abandon"
    OVERRIDE_APPROVE = "override_approve"


_REASON_REQUIRED_ACTIONS = {
    ResumeAction.REVISE,
    ResumeAction.ABANDON,
    ResumeAction.OVERRIDE_APPROVE,
}


@dataclass(frozen=True)
class ResumeRequest:
    """A validated resume request ready to dispatch."""

    action: ResumeAction
    reason: str = ""


def resume_plan(
    feature: str,
    services: PlanningServices,
    request: ResumeRequest,
) -> PlanningGraphResult:
    """Dispatch a resume request to the state machine."""
    _validate_reason(request)
    session = _prepare_planning_session(feature, services, require_fresh=False)

    if request.action is ResumeAction.STATUS:
        return read_planning_state(session)
    if request.action is ResumeAction.APPROVE:
        return resume_awaiting_human_approval(session, "approve")
    if request.action is ResumeAction.REVISE:
        return resume_awaiting_human_approval(session, "revise", request.reason)
    if request.action is ResumeAction.ABANDON:
        return resume_awaiting_human_approval(session, "abandon", request.reason)
    if request.action is ResumeAction.OVERRIDE_APPROVE:
        return override_needs_human_review(session, request.reason)
    raise PlanningError(f"unknown resume action: {request.action}")


def _validate_reason(request: ResumeRequest) -> None:
    if request.action in _REASON_REQUIRED_ACTIONS and not request.reason.strip():
        raise PlanningError(
            f"--{_flag_name(request.action)} requires a non-empty reason"
        )


def _flag_name(action: ResumeAction) -> str:
    if action is ResumeAction.OVERRIDE_APPROVE:
        return "override"
    return action.value
