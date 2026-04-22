"""Tests for the planning state machine."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from turma.authoring.base import AuthorBackend
from turma.planning import PlanningServices, _prepare_planning_session
from turma.planning.state_machine import run_planning_state_machine


CONFIG_TEXT = """\
[planning]
author_model = "claude-opus-4-6"
critic_model = "claude-sonnet-4-6"
"""

PROPOSAL_INSTRUCTIONS = {
    "artifactId": "proposal",
    "outputPath": "proposal.md",
    "instruction": "Create the proposal.",
    "template": "## Why\n\n## What Changes\n",
    "dependencies": [],
}

DESIGN_INSTRUCTIONS = {
    "artifactId": "design",
    "outputPath": "design.md",
    "instruction": "Create the design.",
    "template": "## Goals\n\n## Non-goals\n",
    "dependencies": [{"id": "proposal", "path": "proposal.md"}],
}

TASKS_INSTRUCTIONS = {
    "artifactId": "tasks",
    "outputPath": "tasks.md",
    "instruction": "Create the tasks.",
    "template": "## Task 1\n",
    "dependencies": [{"id": "design", "path": "design.md"}],
}


class FakeBackend(AuthorBackend):
    """Backend test double returning outputs from a callback."""

    def __init__(self, callback):
        self.callback = callback

    def generate(self, prompt: str, model: str, timeout: int) -> str:
        return self.callback(prompt, model, timeout)


def _setup_project(tmp_path: Path) -> None:
    (tmp_path / "turma.toml").write_text(CONFIG_TEXT)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "author.md").write_text("# Author\n")
    (tmp_path / ".agents" / "critic.md").write_text("# Critic\n")
    (tmp_path / "openspec" / "changes").mkdir(parents=True)


def _run_openspec(cmd):
    if cmd[:3] == ["openspec", "new", "change"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    instructions = {
        "proposal": PROPOSAL_INSTRUCTIONS,
        "design": DESIGN_INSTRUCTIONS,
        "tasks": TASKS_INSTRUCTIONS,
    }[cmd[2]]
    return subprocess.CompletedProcess(
        cmd,
        0,
        stdout=json.dumps(instructions),
        stderr="",
    )


def _author_output(prompt: str, model: str, timeout: int) -> str:
    if 'creating the "proposal" artifact' in prompt:
        return "## Why\nNeed it\n\n## What Changes\nAdd it\n"
    if 'creating the "design" artifact' in prompt:
        return "## Goals\nGoal\n\n## Non-goals\nNone\n"
    if 'creating the "tasks" artifact' in prompt:
        return "## Task 1\nDo it\n"
    if "generating the per-finding response artifact" in prompt:
        return "# Response\n\n## [B001] Accept — addressed in revision\n"
    if 'revising the "proposal" artifact' in prompt:
        return "## Why\nRevised need\n\n## What Changes\nRevised add\n"
    if 'revising the "design" artifact' in prompt:
        return "## Goals\nRevised goal\n\n## Non-goals\nRevised none\n"
    if 'revising the "tasks" artifact' in prompt:
        return "## Task 1\nRevised do it\n"
    raise AssertionError(f"unexpected author prompt: {prompt}")


def test_state_machine_suspends_at_human_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1 can checkpoint and suspend before awaiting_human_approval."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_backend = FakeBackend(_author_output)
    critic_backend = FakeBackend(
        lambda *_: (
            "## Status: approved\n\n"
            "## Findings\n"
        )
    )
    services = PlanningServices(
        get_backend=lambda model: (
            critic_backend if model == "claude-sonnet-4-6" else author_backend
        ),
        run_openspec=_run_openspec,
    )

    session = _prepare_planning_session("test-feature", services)
    result = run_planning_state_machine(session)

    change_dir = tmp_path / "openspec" / "changes" / "test-feature"
    state_path = change_dir / "PLANNING_STATE.json"
    state = json.loads(state_path.read_text())

    assert result.state["state"] == "awaiting_human_approval"
    assert result.next_nodes == ("awaiting_human_approval",)
    assert result.checkpoint_path == Path(".langgraph/test-feature.db")
    assert (tmp_path / result.checkpoint_path).exists()
    assert (change_dir / "critique_1.md").exists()
    assert state["feature"] == "test-feature"
    assert state["round"] == 1
    assert state["state"] == "awaiting_human_approval"
    assert state["last_critique"] == "critique_1.md"
    assert state["critic_status"] == "approved"


def test_state_machine_routes_parse_failure_to_human_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed critique output checkpoints needs_human_review."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_backend = FakeBackend(_author_output)
    critic_backend = FakeBackend(lambda *_: "## Status: nope\n")
    services = PlanningServices(
        get_backend=lambda model: (
            critic_backend if model == "claude-sonnet-4-6" else author_backend
        ),
        run_openspec=_run_openspec,
    )

    session = _prepare_planning_session("test-feature", services)
    result = run_planning_state_machine(session)

    state_path = (
        tmp_path
        / "openspec"
        / "changes"
        / "test-feature"
        / "PLANNING_STATE.json"
    )
    state = json.loads(state_path.read_text())

    assert result.state["state"] == "needs_human_review"
    assert result.next_nodes == ()
    assert state["state"] == "needs_human_review"
    assert state["critic_status"] is None


class _SequencedCriticCallback:
    """Critic callback that returns a different response per round."""

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs
        self.calls: int = 0

    def __call__(self, *_args: object, **_kwargs: object) -> str:
        output = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        return output


BLOCKING_CRITIQUE = (
    "## Status: blocking\n\n"
    "## Findings\n"
    "- [B001] [blocking] [design.md] Missing retry rule\n"
)


def test_state_machine_loops_blocking_round_1_to_approved_round_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1 blocking triggers needs_revision → drafting → round-2 critic.

    Task 6 wired the two-call revision path: drafting in round >= 2 first
    produces response_{N-1}.md, then revises the three planning artifacts.
    The round-2 critic here returns approved, so the graph halts at the
    round-2 human gate.
    """
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_backend = FakeBackend(_author_output)
    critic_callback = _SequencedCriticCallback(
        [BLOCKING_CRITIQUE, "## Status: approved\n\n## Findings\n"]
    )
    critic_backend = FakeBackend(critic_callback)
    services = PlanningServices(
        get_backend=lambda model: (
            critic_backend if model == "claude-sonnet-4-6" else author_backend
        ),
        run_openspec=_run_openspec,
    )

    session = _prepare_planning_session("test-feature", services)
    result = run_planning_state_machine(session)

    change_dir = tmp_path / "openspec" / "changes" / "test-feature"
    state = json.loads((change_dir / "PLANNING_STATE.json").read_text())

    assert result.state["state"] == "awaiting_human_approval"
    assert result.next_nodes == ("awaiting_human_approval",)
    assert int(result.state["round"]) == 2

    assert (change_dir / "critique_1.md").exists()
    assert (change_dir / "response_1.md").exists()
    assert (change_dir / "critique_2.md").exists()

    assert state["round"] == 2
    assert state["critic_status"] == "approved"
    assert state["last_critique"] == "critique_2.md"

    # Round 1 initial generation + round 2 revised generation → both critic
    # calls consumed in order.
    assert critic_callback.calls == 2


def test_state_machine_round_two_drafting_reuses_existing_response_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial-failure rule: a response file already on disk is reused.

    Simulates a crash between the two author calls in round 2 by pre-seeding
    response_1.md. The retry should skip the response-generation call and
    only invoke the author for revised drafts (plus the initial round-1
    generation and the two critic calls).
    """
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_call_prompts: list[str] = []

    def _tracked_author_output(prompt: str, model: str, timeout: int) -> str:
        author_call_prompts.append(prompt)
        return _author_output(prompt, model, timeout)

    author_backend = FakeBackend(_tracked_author_output)
    critic_callback = _SequencedCriticCallback(
        [BLOCKING_CRITIQUE, "## Status: approved\n\n## Findings\n"]
    )
    critic_backend = FakeBackend(critic_callback)
    services = PlanningServices(
        get_backend=lambda model: (
            critic_backend if model == "claude-sonnet-4-6" else author_backend
        ),
        run_openspec=_run_openspec,
    )

    session = _prepare_planning_session("test-feature", services)
    change_dir = session.change_dir
    change_dir.mkdir(parents=True, exist_ok=True)
    response_path = change_dir / "response_1.md"
    response_path.write_text("# Response (preseeded from prior attempt)\n")
    preseeded_content = response_path.read_text()

    result = run_planning_state_machine(session)

    # Response content preserved verbatim — response call was skipped.
    assert response_path.read_text() == preseeded_content
    assert result.state["state"] == "awaiting_human_approval"
    assert int(result.state["round"]) == 2

    response_prompts = [
        p for p in author_call_prompts
        if "generating the per-finding response artifact" in p
    ]
    assert response_prompts == [], (
        "response artifact was regenerated despite response_1.md existing"
    )

    revision_prompts = [
        p for p in author_call_prompts if "revising the " in p
    ]
    assert len(revision_prompts) == 3  # proposal, design, tasks all revised
