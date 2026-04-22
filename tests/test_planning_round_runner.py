"""Tests for the minimal author-to-critic planning round runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from turma.authoring.base import AuthorBackend
from turma.planning import (
    PlanningServices,
    _generate_initial_artifacts,
    _prepare_planning_session,
    _run_initial_critic_review,
    _scaffold_change,
)
from turma.planning.critique_parser import ParseFailure, ParsedCritique, RouteDecision


CONFIG_TEXT = """\
[planning]
author_model = "claude-opus-4-6"
critic_model = "claude-sonnet-4-6"
"""

AUTHOR_ROLE = """\
# Author

Purpose: draft OpenSpec artifacts.
"""

CRITIC_ROLE = """\
# Critic

Purpose: review OpenSpec artifacts.
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
        self.calls: list[tuple[str, str, int]] = []

    def generate(self, prompt: str, model: str, timeout: int) -> str:
        self.calls.append((prompt, model, timeout))
        return self.callback(prompt, model, timeout)


def _setup_project(tmp_path: Path) -> None:
    (tmp_path / "turma.toml").write_text(CONFIG_TEXT)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "author.md").write_text(AUTHOR_ROLE)
    (tmp_path / ".agents" / "critic.md").write_text(CRITIC_ROLE)
    (tmp_path / "openspec" / "changes").mkdir(parents=True)


def _run_openspec(cmd):
    if cmd[:3] == ["openspec", "new", "change"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    artifact_id = cmd[2]
    instructions = {
        "proposal": PROPOSAL_INSTRUCTIONS,
        "design": DESIGN_INSTRUCTIONS,
        "tasks": TASKS_INSTRUCTIONS,
    }[artifact_id]
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


@pytest.mark.parametrize(
    ("critic_text", "expected_route"),
    [
        (
            "## Status: blocking\n\n## Findings\n"
            "- [B001] [blocking] [design.md] Missing retry rule\n",
            RouteDecision.NEEDS_REVISION,
        ),
        (
            "## Status: nits_only\n\n## Findings\n"
            "- [N001] [nits] [tasks.md] Split this later\n",
            RouteDecision.AWAITING_HUMAN_APPROVAL,
        ),
        (
            "## Status: approved\n\n## Findings\n",
            RouteDecision.AWAITING_HUMAN_APPROVAL,
        ),
    ],
)
def test_round_runner_writes_and_routes_critique(
    critic_text: str,
    expected_route: RouteDecision,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round runner writes critique_1.md and returns the parsed route."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_backend = FakeBackend(_author_output)
    critic_backend = FakeBackend(lambda *_: critic_text)

    services = PlanningServices(
        get_backend=lambda model: (
            critic_backend if model == "claude-sonnet-4-6" else author_backend
        ),
        run_openspec=_run_openspec,
    )

    session = _prepare_planning_session("test-feature", services)
    _scaffold_change(session)
    artifacts = _generate_initial_artifacts(session)
    result = _run_initial_critic_review(session, artifacts)

    critique_path = (
        tmp_path / "openspec" / "changes" / "test-feature" / "critique_1.md"
    )
    assert critique_path.read_text() == critic_text
    assert isinstance(result, ParsedCritique)
    assert result.route is expected_route


def test_round_runner_returns_parse_failure_for_malformed_critique(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed critic output is written and routed to human review."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_backend = FakeBackend(_author_output)
    critic_backend = FakeBackend(lambda *_: "## Status: looks_good\n")
    services = PlanningServices(
        get_backend=lambda model: (
            critic_backend if model == "claude-sonnet-4-6" else author_backend
        ),
        run_openspec=_run_openspec,
    )

    session = _prepare_planning_session("test-feature", services)
    _scaffold_change(session)
    artifacts = _generate_initial_artifacts(session)
    result = _run_initial_critic_review(session, artifacts)

    critique_path = (
        tmp_path / "openspec" / "changes" / "test-feature" / "critique_1.md"
    )
    assert critique_path.read_text() == "## Status: looks_good\n"
    assert isinstance(result, ParseFailure)
    assert result.route is RouteDecision.NEEDS_HUMAN_REVIEW


def test_round_runner_strips_critic_preamble(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conversational text before the first heading is stripped."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_backend = FakeBackend(_author_output)
    critic_backend = FakeBackend(
        lambda *_: "Here is my review:\n\n## Status: approved\n\n## Findings\n"
    )
    services = PlanningServices(
        get_backend=lambda model: (
            critic_backend if model == "claude-sonnet-4-6" else author_backend
        ),
        run_openspec=_run_openspec,
    )

    session = _prepare_planning_session("test-feature", services)
    _scaffold_change(session)
    artifacts = _generate_initial_artifacts(session)
    result = _run_initial_critic_review(session, artifacts)

    critique_path = (
        tmp_path / "openspec" / "changes" / "test-feature" / "critique_1.md"
    )
    assert critique_path.read_text() == "## Status: approved\n\n## Findings\n"
    assert isinstance(result, ParsedCritique)
    assert result.route is RouteDecision.AWAITING_HUMAN_APPROVAL


def test_critic_prompt_contains_role_and_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critic receives its role plus all generated planning artifacts."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_backend = FakeBackend(_author_output)
    critic_backend = FakeBackend(lambda *_: "## Status: approved\n\n## Findings\n")
    services = PlanningServices(
        get_backend=lambda model: (
            critic_backend if model == "claude-sonnet-4-6" else author_backend
        ),
        run_openspec=_run_openspec,
    )

    session = _prepare_planning_session("test-feature", services)
    _scaffold_change(session)
    artifacts = _generate_initial_artifacts(session)
    _run_initial_critic_review(session, artifacts)

    critic_prompt = critic_backend.calls[0][0]
    assert "Purpose: review OpenSpec artifacts" in critic_prompt
    assert "<proposal>\n## Why\nNeed it" in critic_prompt
    assert "<design>\n## Goals\nGoal" in critic_prompt
    assert "<tasks>\n## Task 1\nDo it" in critic_prompt
