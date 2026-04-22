"""Tests for the planning resume CLI dispatch and state machine primitives."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from turma.authoring.base import AuthorBackend
from turma.errors import PlanningError
from turma.planning import PlanningServices, _prepare_planning_session
from turma.planning.resume import ResumeAction, ResumeRequest, resume_plan
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

    def __init__(self, callback: Callable[[str, str, int], str]) -> None:
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


def _build_services(critic_text: str) -> PlanningServices:
    author_backend = FakeBackend(_author_output)
    critic_backend = FakeBackend(lambda *_: critic_text)
    return PlanningServices(
        get_backend=lambda model: (
            critic_backend if model == "claude-sonnet-4-6" else author_backend
        ),
        run_openspec=_run_openspec,
    )


def _seed_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    critic_text: str,
) -> PlanningServices:
    """Create the project layout and run the state machine once."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    services = _build_services(critic_text)
    session = _prepare_planning_session("test-feature", services)
    run_planning_state_machine(session)
    return services


APPROVED_CRITIQUE = "## Status: approved\n\n## Findings\n"
MALFORMED_CRITIQUE = "## Status: nope\n"


def test_status_is_read_only_at_awaiting_human_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--resume alone reports state without writing markers or advancing."""
    services = _seed_project(tmp_path, monkeypatch, APPROVED_CRITIQUE)
    change_dir = tmp_path / "openspec" / "changes" / "test-feature"
    existing_before = {p.name for p in change_dir.iterdir()}

    result = resume_plan(
        "test-feature",
        services,
        ResumeRequest(action=ResumeAction.STATUS),
    )

    assert result.state["state"] == "awaiting_human_approval"
    assert result.next_nodes == ("awaiting_human_approval",)
    assert {p.name for p in change_dir.iterdir()} == existing_before
    assert not (change_dir / "APPROVED").exists()


def test_resume_approve_writes_marker_and_transitions_to_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--approve writes APPROVED and moves the graph to the approved terminal."""
    services = _seed_project(tmp_path, monkeypatch, APPROVED_CRITIQUE)
    change_dir = tmp_path / "openspec" / "changes" / "test-feature"

    result = resume_plan(
        "test-feature",
        services,
        ResumeRequest(action=ResumeAction.APPROVE),
    )

    assert result.state["state"] == "approved"
    assert result.next_nodes == ()
    assert (change_dir / "APPROVED").read_text().startswith("approved by human")
    state = json.loads((change_dir / "PLANNING_STATE.json").read_text())
    assert state["state"] == "approved"


def test_resume_revise_writes_human_reason_and_loops_to_round_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--revise writes response_N_human.md and drives the graph into round 2.

    Task 6 wired needs_revision → drafting, so --revise now loops through a
    round-2 author two-call revision and a fresh critic_review, halting at
    the round-2 human gate.
    """
    services = _seed_project(tmp_path, monkeypatch, APPROVED_CRITIQUE)
    change_dir = tmp_path / "openspec" / "changes" / "test-feature"

    result = resume_plan(
        "test-feature",
        services,
        ResumeRequest(action=ResumeAction.REVISE, reason="too vague"),
    )

    assert result.state["state"] == "awaiting_human_approval"
    assert result.next_nodes == ("awaiting_human_approval",)
    assert int(result.state["round"]) == 2

    reason_path = change_dir / "response_1_human.md"
    assert "too vague" in reason_path.read_text()
    assert (change_dir / "response_1.md").exists()
    assert (change_dir / "critique_2.md").exists()
    state = json.loads((change_dir / "PLANNING_STATE.json").read_text())
    assert state["state"] == "awaiting_human_approval"
    assert state["round"] == 2


def test_resume_abandon_writes_marker_and_transitions_to_abandoned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--abandon writes ABANDONED.md and moves the graph to abandoned."""
    services = _seed_project(tmp_path, monkeypatch, APPROVED_CRITIQUE)
    change_dir = tmp_path / "openspec" / "changes" / "test-feature"

    result = resume_plan(
        "test-feature",
        services,
        ResumeRequest(action=ResumeAction.ABANDON, reason="pivoted"),
    )

    assert result.state["state"] == "abandoned"
    assert result.next_nodes == ()
    marker_text = (change_dir / "ABANDONED.md").read_text()
    assert "pivoted" in marker_text
    assert "round: 1" in marker_text
    assert "actor: human" in marker_text
    state = json.loads((change_dir / "PLANNING_STATE.json").read_text())
    assert state["state"] == "abandoned"


def test_override_approve_from_needs_human_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--approve --override moves needs_human_review into approved."""
    services = _seed_project(tmp_path, monkeypatch, MALFORMED_CRITIQUE)
    change_dir = tmp_path / "openspec" / "changes" / "test-feature"
    assert (change_dir / "NEEDS_HUMAN_REVIEW.md").exists()

    result = resume_plan(
        "test-feature",
        services,
        ResumeRequest(
            action=ResumeAction.OVERRIDE_APPROVE,
            reason="critic mis-parsed valid status",
        ),
    )

    assert result.state["state"] == "approved"
    override_text = (change_dir / "OVERRIDE.md").read_text()
    assert "critic mis-parsed valid status" in override_text
    assert (change_dir / "APPROVED").exists()
    state = json.loads((change_dir / "PLANNING_STATE.json").read_text())
    assert state["state"] == "approved"


def test_override_writes_override_before_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OVERRIDE.md must predate APPROVED for fail-safe recovery."""
    services = _seed_project(tmp_path, monkeypatch, MALFORMED_CRITIQUE)
    change_dir = tmp_path / "openspec" / "changes" / "test-feature"

    resume_plan(
        "test-feature",
        services,
        ResumeRequest(
            action=ResumeAction.OVERRIDE_APPROVE,
            reason="manual unblock",
        ),
    )

    override_mtime = (change_dir / "OVERRIDE.md").stat().st_mtime_ns
    approved_mtime = (change_dir / "APPROVED").stat().st_mtime_ns
    assert override_mtime <= approved_mtime


def test_override_rejected_from_awaiting_human_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--override is only valid from halted needs_human_review."""
    services = _seed_project(tmp_path, monkeypatch, APPROVED_CRITIQUE)

    with pytest.raises(PlanningError, match="needs_human_review"):
        resume_plan(
            "test-feature",
            services,
            ResumeRequest(
                action=ResumeAction.OVERRIDE_APPROVE,
                reason="trying too early",
            ),
        )


def test_approve_rejected_from_needs_human_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain --approve is not allowed once the graph halts in needs_human_review."""
    services = _seed_project(tmp_path, monkeypatch, MALFORMED_CRITIQUE)

    with pytest.raises(PlanningError, match="awaiting_human_approval"):
        resume_plan(
            "test-feature",
            services,
            ResumeRequest(action=ResumeAction.APPROVE),
        )


def test_resume_without_prior_checkpoint_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume requires a prior turma plan run."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    # Create change_dir manually so _prepare_planning_session gets past the
    # existence check, then attempt resume with no checkpoint on disk.
    (tmp_path / "openspec" / "changes" / "test-feature").mkdir()

    services = _build_services(APPROVED_CRITIQUE)

    with pytest.raises(PlanningError, match="no planning checkpoint"):
        resume_plan(
            "test-feature",
            services,
            ResumeRequest(action=ResumeAction.STATUS),
        )


def test_resume_fails_when_change_dir_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume requires openspec/changes/<feature>/ to already exist."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    services = _build_services(APPROVED_CRITIQUE)

    with pytest.raises(PlanningError, match="does not exist"):
        resume_plan(
            "test-feature",
            services,
            ResumeRequest(action=ResumeAction.STATUS),
        )


@pytest.mark.parametrize(
    "action",
    [
        ResumeAction.REVISE,
        ResumeAction.ABANDON,
        ResumeAction.OVERRIDE_APPROVE,
    ],
)
def test_reason_required_for_actions(
    action: ResumeAction,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revise, abandon, and override-approve all require a non-empty reason."""
    services = _seed_project(tmp_path, monkeypatch, APPROVED_CRITIQUE)

    with pytest.raises(PlanningError, match="non-empty reason"):
        resume_plan(
            "test-feature",
            services,
            ResumeRequest(action=action, reason="   "),
        )
