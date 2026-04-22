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


def test_generate_round_revision_reuses_existing_response_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial-failure rule: a response_{N-1}.md already on disk is reused.

    This is a direct unit test of `_generate_round_revision` — the function
    that implements the partial-failure guard. It does not go through the
    LangGraph state machine or its checkpointing. End-to-end checkpoint
    recovery across crashes is Task 7 scope; this test asserts only that
    the reuse logic inside `_generate_round_revision` does the right
    thing when called with a pre-existing response file.
    """
    from turma.planning import _generate_round_revision

    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_prompts: list[str] = []

    def _tracked_author(prompt: str, model: str, timeout: int) -> str:
        author_prompts.append(prompt)
        return _author_output(prompt, model, timeout)

    author_backend = FakeBackend(_tracked_author)
    critic_backend = FakeBackend(lambda *_: "## Status: approved\n\n## Findings\n")
    services = PlanningServices(
        get_backend=lambda model: (
            critic_backend if model == "claude-sonnet-4-6" else author_backend
        ),
        run_openspec=_run_openspec,
    )

    # Build a session and pre-populate the change directory as if round 1
    # had completed and a partial round 2 crashed after writing response_1.md.
    session = _prepare_planning_session("test-feature", services)
    change_dir = session.change_dir
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "proposal.md").write_text("## Why\nNeed it\n\n## What Changes\nAdd it\n")
    (change_dir / "design.md").write_text("## Goals\nGoal\n\n## Non-goals\nNone\n")
    (change_dir / "tasks.md").write_text("## Task 1\nDo it\n")
    (change_dir / "critique_1.md").write_text(BLOCKING_CRITIQUE)
    preseeded = "# Response (preseeded from prior crashed attempt)\n"
    (change_dir / "response_1.md").write_text(preseeded)

    _generate_round_revision(session, round_num=2)

    # Response file must be left untouched — no second write.
    assert (change_dir / "response_1.md").read_text() == preseeded

    # Author was only called for the three revised artifacts — no response
    # prompt was ever sent.
    response_prompts = [
        p for p in author_prompts
        if "generating the per-finding response artifact" in p
    ]
    assert response_prompts == [], (
        "response artifact was regenerated despite response_1.md existing"
    )

    revision_prompts = [p for p in author_prompts if "revising the " in p]
    assert len(revision_prompts) == 3  # proposal, design, tasks all revised


def test_needs_revision_routes_to_human_review_when_max_rounds_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Temporary guard: blocking loop halts when max_rounds is exceeded.

    Task 7 will implement full loop detection (repeated unresolved blocking
    finding ID sets). Until then, `_needs_revision_node` caps the loop at
    `session.max_rounds`: attempting to advance into round > max_rounds
    routes to `needs_human_review` with a max_rounds reason and writes
    NEEDS_HUMAN_REVIEW.md.
    """
    _setup_project(tmp_path)
    # Override config to cap at 2 rounds, so round 1 blocking → round 2
    # blocking → max_rounds guard halts before round 3.
    (tmp_path / "turma.toml").write_text(
        "[planning]\n"
        'author_model = "claude-opus-4-6"\n'
        'critic_model = "claude-sonnet-4-6"\n'
        "max_rounds = 2\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    # Distinct blocking finding IDs per round so loop-detection (which
    # fires on identical unresolved blocking ID sets) does not short-circuit
    # the max_rounds guard.
    author_backend = FakeBackend(_author_output)
    critic_callback = _SequencedCriticCallback(
        [
            BLOCKING_CRITIQUE,
            (
                "## Status: blocking\n\n"
                "## Findings\n"
                "- [B002] [blocking] [design.md] Different blocker round 2\n"
            ),
        ]
    )
    critic_backend = FakeBackend(critic_callback)
    services = PlanningServices(
        get_backend=lambda model: (
            critic_backend if model == "claude-sonnet-4-6" else author_backend
        ),
        run_openspec=_run_openspec,
    )

    session = _prepare_planning_session("test-feature", services)
    assert session.max_rounds == 2

    result = run_planning_state_machine(session)

    change_dir = tmp_path / "openspec" / "changes" / "test-feature"
    assert result.state["state"] == "needs_human_review"
    assert result.next_nodes == ()
    assert "max_rounds" in result.state.get("needs_human_review_reason", "")
    needs_review = (change_dir / "NEEDS_HUMAN_REVIEW.md").read_text()
    assert "max_rounds" in needs_review


def test_loop_detection_halts_on_repeated_blocking_finding_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive rounds with identical unresolved blocking IDs halts.

    Loop detection uses the set of unresolved blocking finding IDs per
    round. If the critic re-flags exactly the same IDs two rounds in a
    row, the author is not making progress and the plan escalates to
    needs_human_review with a loop-detection reason.
    """
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_backend = FakeBackend(_author_output)
    critic_callback = _SequencedCriticCallback(
        [BLOCKING_CRITIQUE, BLOCKING_CRITIQUE]  # same [B001] both rounds
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
    assert result.state["state"] == "needs_human_review"
    assert result.next_nodes == ()
    reason = result.state.get("needs_human_review_reason", "")
    assert "repeated" in reason
    assert "B001" in reason
    needs_review = (change_dir / "NEEDS_HUMAN_REVIEW.md").read_text()
    assert "repeated" in needs_review


def test_loop_detection_ignores_different_blocking_finding_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different blocking finding IDs across rounds do not trigger a halt.

    If the critic resolves one finding and raises a new one, the
    unresolved set changes — the author is making progress. The loop
    should continue normally.
    """
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_backend = FakeBackend(_author_output)
    critic_callback = _SequencedCriticCallback(
        [
            BLOCKING_CRITIQUE,  # [B001]
            (
                "## Status: blocking\n\n"
                "## Findings\n"
                "- [B002] [blocking] [design.md] New round-2 blocker\n"
            ),
            "## Status: approved\n\n## Findings\n",  # round 3 clears
        ]
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

    # Loop did NOT detect repeat — advanced through to round 3 approved.
    assert result.state["state"] == "awaiting_human_approval"
    assert int(result.state["round"]) == 3
    assert critic_callback.calls == 3


def test_reconcile_current_state_detects_terminal_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal markers on disk are reported by reconcile_current_state."""
    from turma.planning.state_machine import reconcile_current_state

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
    session.change_dir.mkdir(parents=True, exist_ok=True)

    # No markers yet.
    assert reconcile_current_state(session) is None

    (session.change_dir / "NEEDS_HUMAN_REVIEW.md").write_text("stub\n")
    assert reconcile_current_state(session) == "needs_human_review"

    (session.change_dir / "ABANDONED.md").write_text("stub\n")
    # ABANDONED comes before NEEDS_HUMAN_REVIEW in the authority order.
    assert reconcile_current_state(session) == "abandoned"

    (session.change_dir / "APPROVED").write_text("stub\n")
    # APPROVED wins over both.
    assert reconcile_current_state(session) == "approved"


def test_run_state_machine_short_circuits_on_terminal_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An APPROVED file on disk halts the state machine without invoking it.

    Terminal markers are authoritative: if the plan is already approved,
    re-running `turma plan` must not re-invoke the author or critic.
    """
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("turma.planning.shutil.which", lambda _: "/usr/bin/mock")

    author_calls: list[str] = []
    critic_calls: list[str] = []

    def _tracked_author(prompt: str, *_: object) -> str:
        author_calls.append(prompt)
        return "should not be called"

    def _tracked_critic(prompt: str, *_: object) -> str:
        critic_calls.append(prompt)
        return "should not be called"

    author_backend = FakeBackend(_tracked_author)
    critic_backend = FakeBackend(_tracked_critic)
    services = PlanningServices(
        get_backend=lambda model: (
            critic_backend if model == "claude-sonnet-4-6" else author_backend
        ),
        run_openspec=_run_openspec,
    )
    session = _prepare_planning_session("test-feature", services)
    session.change_dir.mkdir(parents=True, exist_ok=True)
    (session.change_dir / "APPROVED").write_text(
        "approved manually for this test\n"
    )

    result = run_planning_state_machine(session)

    assert result.state["state"] == "approved"
    assert result.next_nodes == ()
    assert author_calls == [], "author was invoked despite APPROVED marker"
    assert critic_calls == [], "critic was invoked despite APPROVED marker"


def test_planning_state_json_is_not_authoritative_for_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLANNING_STATE.json is a hint; it cannot by itself flip state to terminal.

    A JSON file claiming state=approved with no corresponding APPROVED
    marker must NOT cause the state machine to short-circuit. Only
    filesystem terminal markers trigger reconciliation.
    """
    from turma.planning.state_machine import reconcile_current_state

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
    session.change_dir.mkdir(parents=True, exist_ok=True)

    # JSON claims approved but no APPROVED marker on disk.
    (session.change_dir / "PLANNING_STATE.json").write_text(
        json.dumps({"feature": "test-feature", "state": "approved"}) + "\n"
    )

    assert reconcile_current_state(session) is None


def test_interactive_false_halts_and_prints_resume_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With interactive=false the planner halts at the human gate cleanly.

    Confirms the documented behavior: the graph halts at
    awaiting_human_approval, prints the exact resume commands the human
    would use, and exits without auto-approving.
    """
    from turma.planning import run_planning

    (tmp_path / "turma.toml").write_text(
        "[planning]\n"
        'author_model = "claude-opus-4-6"\n'
        'critic_model = "claude-sonnet-4-6"\n'
        "interactive = false\n"
    )
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "author.md").write_text("# Author\n")
    (tmp_path / ".agents" / "critic.md").write_text("# Critic\n")
    (tmp_path / "openspec" / "changes").mkdir(parents=True)
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

    run_planning("test-feature", services)

    out = capsys.readouterr().out
    assert "planning suspended before: awaiting_human_approval" in out
    assert "--resume --approve" in out
    assert "--resume --revise" in out
    assert "--resume --abandon" in out
    assert "planning complete" not in out  # explicitly not auto-approved

    change_dir = tmp_path / "openspec" / "changes" / "test-feature"
    assert not (change_dir / "APPROVED").exists(), (
        "non-interactive halt must not auto-approve"
    )
