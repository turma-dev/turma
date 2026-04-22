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


def test_state_machine_routes_blocking_to_needs_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocking critiques route to needs_revision without a human gate."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_backend = FakeBackend(_author_output)
    critic_backend = FakeBackend(
        lambda *_: (
            "## Status: blocking\n\n"
            "## Findings\n"
            "- [B001] [blocking] [design.md] Missing retry rule\n"
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

    state_path = (
        tmp_path
        / "openspec"
        / "changes"
        / "test-feature"
        / "PLANNING_STATE.json"
    )
    state = json.loads(state_path.read_text())

    assert result.state["state"] == "needs_revision"
    assert result.next_nodes == ()
    assert state["state"] == "needs_revision"
    assert state["critic_status"] == "blocking"
