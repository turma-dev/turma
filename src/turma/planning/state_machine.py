"""LangGraph state machine for the planning critic loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from turma.planning import (
    PlanningSession,
    _generate_initial_artifacts,
    _print_critic_result,
    _run_initial_critic_review,
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


class PlanningGraphState(TypedDict, total=False):
    """Serializable state stored by the planning graph checkpointer."""

    feature: str
    round: int
    state: PlanningStateName
    critic_status: str
    critic_route: str
    parse_failure_reason: str
    last_critique: str


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
    graph.add_node("needs_revision", _halt_node("needs_revision"))
    graph.add_node("awaiting_human_approval", _halt_node("awaiting_human_approval"))
    graph.add_node("needs_human_review", _halt_node("needs_human_review"))
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
    # Task 6 wires needs_revision back to drafting after two-call revision.
    graph.add_edge("needs_revision", END)
    graph.add_edge("awaiting_human_approval", END)
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
    _scaffold_change(session)
    _generate_initial_artifacts(session)
    updated = {**state, "state": "critic_review"}
    _write_planning_state(session, updated)
    return updated


def _critic_review_node(
    session: PlanningSession,
    state: PlanningGraphState,
) -> PlanningGraphState:
    artifact_paths = {
        "proposal": session.change_dir / "proposal.md",
        "design": session.change_dir / "design.md",
        "tasks": session.change_dir / "tasks.md",
    }
    critique = _run_initial_critic_review(session, artifact_paths)
    _print_critic_result(critique)

    next_state = _state_name_for_critique(critique)
    updated: PlanningGraphState = {
        **state,
        "state": next_state,
        "critic_route": critique.route.value,
        "last_critique": "critique_1.md",
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
