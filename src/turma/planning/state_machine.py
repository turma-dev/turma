"""LangGraph state machine for the planning critic loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from turma.errors import PlanningError
from turma.planning import (
    PlanningSession,
    _generate_initial_artifacts,
    _generate_round_revision,
    _print_critic_result,
    _run_critic_review,
    _scaffold_change,
)
from turma.planning.critique_parser import CritiqueStatus, ParseFailure, ParsedCritique

PlanningStateName = Literal[
    "drafting",
    "critic_review",
    "needs_revision",
    "awaiting_human_approval",
    "needs_human_review",
    "approved",
    "abandoned",
]

ResumeActionName = Literal["approve", "revise", "abandon"]


class PlanningGraphState(TypedDict, total=False):
    """Serializable state stored by the planning graph checkpointer."""

    feature: str
    round: int
    state: PlanningStateName
    critic_status: str
    critic_route: str
    parse_failure_reason: str
    last_critique: str
    resume_action: ResumeActionName | None
    resume_reason: str | None


@dataclass(frozen=True)
class PlanningGraphResult:
    """Result of running the graph until completion or interruption."""

    state: PlanningGraphState
    next_nodes: tuple[str, ...]
    checkpoint_path: Path


def checkpoint_path_for(feature: str) -> Path:
    """Return the SQLite checkpoint path for a feature."""
    return Path(".langgraph") / f"{feature}.db"


def run_planning_state_machine(session: PlanningSession) -> PlanningGraphResult:
    """Run round 1 through critic review and suspend at the human gate."""
    checkpoint_path = checkpoint_path_for(session.feature)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        graph = _build_planning_graph(session).compile(
            checkpointer=checkpointer,
            interrupt_before=["awaiting_human_approval"],
        )
        config = {"configurable": {"thread_id": session.feature}}
        state = graph.invoke(_initial_state(session), config)
        snapshot = graph.get_state(config)

    return PlanningGraphResult(
        state=state,
        next_nodes=tuple(snapshot.next),
        checkpoint_path=checkpoint_path,
    )


def _build_planning_graph(session: PlanningSession) -> StateGraph:
    graph = StateGraph(PlanningGraphState)
    graph.add_node("drafting", lambda state: _drafting_node(session, state))
    graph.add_node("critic_review", lambda state: _critic_review_node(session, state))
    graph.add_node(
        "needs_revision",
        lambda state: _needs_revision_node(session, state),
    )
    graph.add_node(
        "awaiting_human_approval",
        lambda state: _awaiting_human_approval_node(session, state),
    )
    graph.add_node(
        "needs_human_review",
        lambda state: _needs_human_review_node(session, state),
    )
    graph.add_node("approved", _halt_node("approved"))
    graph.add_node("abandoned", _halt_node("abandoned"))

    graph.add_edge(START, "drafting")
    graph.add_edge("drafting", "critic_review")
    graph.add_conditional_edges(
        "critic_review",
        _route_after_critic_review,
        {
            "needs_revision": "needs_revision",
            "awaiting_human_approval": "awaiting_human_approval",
            "needs_human_review": "needs_human_review",
        },
    )
    graph.add_conditional_edges(
        "awaiting_human_approval",
        _route_after_human_approval,
        {
            "approved": "approved",
            "needs_revision": "needs_revision",
            "abandoned": "abandoned",
        },
    )
    graph.add_edge("needs_revision", "drafting")
    graph.add_edge("needs_human_review", END)
    graph.add_edge("approved", END)
    graph.add_edge("abandoned", END)
    return graph


def _initial_state(session: PlanningSession) -> PlanningGraphState:
    return {
        "feature": session.feature,
        "round": 1,
        "state": "drafting",
    }


def _drafting_node(
    session: PlanningSession,
    state: PlanningGraphState,
) -> PlanningGraphState:
    round_num = int(state.get("round", 1))
    if round_num == 1:
        _scaffold_change(session)
        _generate_initial_artifacts(session)
    else:
        _generate_round_revision(session, round_num)
    updated = {**state, "state": "critic_review"}
    _write_planning_state(session, updated)
    return updated


def _needs_revision_node(
    session: PlanningSession,
    state: PlanningGraphState,
) -> PlanningGraphState:
    next_round = int(state.get("round", 1)) + 1
    updated: PlanningGraphState = {
        **state,
        "round": next_round,
        "state": "drafting",
    }
    _write_planning_state(session, updated)
    return {"round": next_round, "state": "drafting"}


def _critic_review_node(
    session: PlanningSession,
    state: PlanningGraphState,
) -> PlanningGraphState:
    round_num = int(state.get("round", 1))
    artifact_paths = {
        "proposal": session.change_dir / "proposal.md",
        "design": session.change_dir / "design.md",
        "tasks": session.change_dir / "tasks.md",
    }
    critique = _run_critic_review(session, artifact_paths, round_num=round_num)
    _print_critic_result(critique)

    next_state = _state_name_for_critique(critique)
    updated: PlanningGraphState = {
        **state,
        "state": next_state,
        "critic_route": critique.route.value,
        "last_critique": f"critique_{round_num}.md",
    }

    if isinstance(critique, ParsedCritique):
        updated["critic_status"] = critique.status.value
    else:
        updated["parse_failure_reason"] = critique.reason

    _write_planning_state(session, updated)
    return updated


def _state_name_for_critique(critique: ParsedCritique | ParseFailure) -> PlanningStateName:
    if isinstance(critique, ParseFailure):
        return "needs_human_review"
    if critique.status is CritiqueStatus.BLOCKING:
        return "needs_revision"
    return "awaiting_human_approval"


def _route_after_critic_review(state: PlanningGraphState) -> str:
    return state["state"]


def _halt_node(state_name: PlanningStateName):
    def node(state: PlanningGraphState) -> PlanningGraphState:
        updated: PlanningGraphState = {**state, "state": state_name}
        return updated

    return node


def _write_planning_state(
    session: PlanningSession,
    state: PlanningGraphState,
) -> None:
    payload = {
        "feature": session.feature,
        "round": state.get("round", 1),
        "state": state.get("state"),
        # Task 7 will wire this to the last round-level git commit SHA.
        "last_commit": None,
        "last_critique": state.get("last_critique"),
        "critic_status": state.get("critic_status"),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    path = session.change_dir / "PLANNING_STATE.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _awaiting_human_approval_node(
    session: PlanningSession,
    state: PlanningGraphState,
) -> PlanningGraphState:
    action = state.get("resume_action")
    reason = state.get("resume_reason") or ""
    round_num = int(state.get("round", 1))

    if action == "approve":
        _write_approved(session)
        next_state: PlanningGraphState = {"state": "approved"}
    elif action == "revise":
        _write_response_human(session, reason, round_num)
        next_state = {"state": "needs_revision"}
    elif action == "abandon":
        _write_abandoned(session, reason, round_num)
        next_state = {"state": "abandoned"}
    else:
        raise PlanningError(
            "awaiting_human_approval reached without a resume action; "
            "did you call update_state with resume_action before invoke?"
        )

    updated: PlanningGraphState = {
        **state,
        **next_state,
        "resume_action": None,
        "resume_reason": None,
    }
    _write_planning_state(session, updated)
    return {**next_state, "resume_action": None, "resume_reason": None}


def _needs_human_review_node(
    session: PlanningSession,
    state: PlanningGraphState,
) -> PlanningGraphState:
    reason = state.get("parse_failure_reason") or "See PLANNING_STATE.json"
    round_num = int(state.get("round", 1))
    _write_needs_human_review(session, reason, round_num)
    return {"state": "needs_human_review"}


def _route_after_human_approval(state: PlanningGraphState) -> str:
    return state["state"]


def _write_approved(session: PlanningSession) -> Path:
    path = session.change_dir / "APPROVED"
    path.write_text(
        f"approved by human on {datetime.now(UTC).isoformat()}\n"
    )
    return path


def _write_abandoned(
    session: PlanningSession,
    reason: str,
    round_num: int,
    actor: str = "human",
) -> Path:
    path = session.change_dir / "ABANDONED.md"
    path.write_text(
        "# ABANDONED\n\n"
        f"- reason: {reason}\n"
        f"- timestamp: {datetime.now(UTC).isoformat()}\n"
        f"- round: {round_num}\n"
        f"- actor: {actor}\n"
    )
    return path


def _write_override(session: PlanningSession, reason: str) -> Path:
    path = session.change_dir / "OVERRIDE.md"
    path.write_text(
        "# OVERRIDE\n\n"
        f"- reason: {reason}\n"
        f"- timestamp: {datetime.now(UTC).isoformat()}\n"
    )
    return path


def _write_response_human(
    session: PlanningSession,
    reason: str,
    round_num: int,
) -> Path:
    path = session.change_dir / f"response_{round_num}_human.md"
    path.write_text(
        f"# Human revision reason (round {round_num})\n\n{reason}\n"
    )
    return path


def _write_needs_human_review(
    session: PlanningSession,
    reason: str,
    round_num: int,
) -> Path:
    path = session.change_dir / "NEEDS_HUMAN_REVIEW.md"
    path.write_text(
        "# NEEDS HUMAN REVIEW\n\n"
        f"- reason: {reason}\n"
        f"- timestamp: {datetime.now(UTC).isoformat()}\n"
        f"- round: {round_num}\n"
    )
    return path


def read_planning_state(session: PlanningSession) -> PlanningGraphResult:
    """Load the checkpoint and return current state without mutating."""
    checkpoint_path = checkpoint_path_for(session.feature)
    if not checkpoint_path.exists():
        raise PlanningError(
            f"no planning checkpoint found for {session.feature!r}. "
            "Run turma plan first."
        )
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        graph = _build_planning_graph(session).compile(
            checkpointer=checkpointer,
            interrupt_before=["awaiting_human_approval"],
        )
        config = {"configurable": {"thread_id": session.feature}}
        snapshot = graph.get_state(config)

    return PlanningGraphResult(
        state=snapshot.values,
        next_nodes=tuple(snapshot.next),
        checkpoint_path=checkpoint_path,
    )


def resume_awaiting_human_approval(
    session: PlanningSession,
    action: ResumeActionName,
    reason: str = "",
) -> PlanningGraphResult:
    """Inject a resume action and drive the graph past awaiting_human_approval."""
    checkpoint_path = checkpoint_path_for(session.feature)
    if not checkpoint_path.exists():
        raise PlanningError(
            f"no planning checkpoint found for {session.feature!r}. "
            "Run turma plan first."
        )
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        graph = _build_planning_graph(session).compile(
            checkpointer=checkpointer,
            interrupt_before=["awaiting_human_approval"],
        )
        config = {"configurable": {"thread_id": session.feature}}
        snapshot = graph.get_state(config)

        if "awaiting_human_approval" not in snapshot.next:
            current = snapshot.values.get("state")
            raise PlanningError(
                f"--{action} requires the graph suspended at "
                f"awaiting_human_approval (current state: {current!r})"
            )

        graph.update_state(
            config,
            {"resume_action": action, "resume_reason": reason},
        )
        final_state = graph.invoke(None, config)
        final_snapshot = graph.get_state(config)

    return PlanningGraphResult(
        state=final_state,
        next_nodes=tuple(final_snapshot.next),
        checkpoint_path=checkpoint_path,
    )


def override_needs_human_review(
    session: PlanningSession,
    reason: str,
) -> PlanningGraphResult:
    """Write OVERRIDE.md then APPROVED and move the graph state to approved."""
    checkpoint_path = checkpoint_path_for(session.feature)
    if not checkpoint_path.exists():
        raise PlanningError(
            f"no planning checkpoint found for {session.feature!r}. "
            "Run turma plan first."
        )
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        graph = _build_planning_graph(session).compile(
            checkpointer=checkpointer,
            interrupt_before=["awaiting_human_approval"],
        )
        config = {"configurable": {"thread_id": session.feature}}
        snapshot = graph.get_state(config)

        current = snapshot.values.get("state")
        if current != "needs_human_review":
            raise PlanningError(
                f"--override requires current state needs_human_review "
                f"(got {current!r})"
            )

        # Fail-safe order: OVERRIDE.md first, APPROVED second. If the second
        # write crashes, recovery sees an orphaned OVERRIDE.md and treats
        # the plan as still-unapproved pending re-confirmation.
        _write_override(session, reason)
        _write_approved(session)

        graph.update_state(config, {"state": "approved"})
        new_snapshot = graph.get_state(config)
        _write_planning_state(session, new_snapshot.values)

    return PlanningGraphResult(
        state=new_snapshot.values,
        next_nodes=tuple(new_snapshot.next),
        checkpoint_path=checkpoint_path,
    )
