"""Tests for turma plan orchestration."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from turma.errors import PlanningError
from turma.planning import (
    _build_prompt,
    _build_repo_context,
    _extract_template_headings,
    _extract_instructions_json,
    _get_backend,
    _strip_leading_preamble,
    _strip_wrapping_code_fence,
    _validate_artifact_output,
    run_planning,
)


VALID_CONFIG_TEXT = """\
[planning]
author_model = "claude-opus-4-6"
critic_model = "claude-sonnet-4-6"
"""

AUTHOR_ROLE = """\
# Author

Purpose: draft and revise OpenSpec artifacts.
"""

CRITIC_ROLE = """\
# Critic

Purpose: review OpenSpec artifacts.
"""

PROPOSAL_INSTRUCTIONS = {
    "artifactId": "proposal",
    "outputPath": "proposal.md",
    "instruction": "Create the proposal document.",
    "template": "## Why\n\n## What Changes\n",
    "dependencies": [],
}

DESIGN_INSTRUCTIONS = {
    "artifactId": "design",
    "outputPath": "design.md",
    "instruction": "Create the design document.",
    "template": "## Goals\n\n## Non-goals\n",
    "dependencies": [
        {"id": "proposal", "done": True, "path": "proposal.md"}
    ],
}

TASKS_INSTRUCTIONS = {
    "artifactId": "tasks",
    "outputPath": "tasks.md",
    "instruction": "Create the tasks document.",
    "template": "## Task 1\n",
    "dependencies": [
        {"id": "design", "done": True, "path": "design.md"},
        {"id": "specs", "done": False, "path": "specs/**/*.md"},
    ],
}


def _make_openspec_output(instructions_dict: dict) -> str:
    """Simulate openspec instructions output: progress line + JSON."""
    return "- Generating instructions...\n" + json.dumps(instructions_dict)


def _setup_project(tmp_path: Path) -> None:
    """Create the minimal project structure for turma plan."""
    (tmp_path / "turma.toml").write_text(VALID_CONFIG_TEXT)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "author.md").write_text(AUTHOR_ROLE)
    (tmp_path / ".agents" / "critic.md").write_text(CRITIC_ROLE)
    (tmp_path / "openspec" / "changes").mkdir(parents=True)


def _mock_openspec(feature: str):
    """Return a side_effect function for openspec subprocess calls."""
    def side_effect(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""

        if cmd[0] == "openspec" and cmd[1] == "new":
            return result
        if cmd[0] == "openspec" and cmd[1] == "instructions":
            artifact_id = cmd[2]
            mapping = {
                "proposal": PROPOSAL_INSTRUCTIONS,
                "design": DESIGN_INSTRUCTIONS,
                "tasks": TASKS_INSTRUCTIONS,
            }
            result.stdout = _make_openspec_output(mapping[artifact_id])
            return result

        return result

    return side_effect


def _artifact_output_from_prompt(
    prompt: str,
    model: str,
    timeout: int,
) -> str:
    """Return artifact-shaped markdown based on the prompt contents."""
    if 'creating the "proposal" artifact' in prompt:
        return "## Why\nText\n\n## What Changes\nStuff\n"
    if 'creating the "design" artifact' in prompt:
        return "## Goals\nGoal\n\n## Non-goals\nNone\n"
    if 'creating the "tasks" artifact' in prompt:
        return "## Task 1\nDo thing\n"
    if "Review the round 1 planning artifacts" in prompt:
        return "## Status: approved\n\n## Findings\n"
    return "## Unknown\nText\n"


def test_fails_when_turma_toml_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """turma plan fails clearly when turma.toml is missing."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PlanningError, match="turma.toml"):
        run_planning("some-feature")


def test_fails_when_author_md_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """turma plan fails when .agents/author.md is missing."""
    (tmp_path / "turma.toml").write_text(VALID_CONFIG_TEXT)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PlanningError, match="author.md"):
        run_planning("some-feature")


def test_fails_when_critic_md_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """turma plan fails when .agents/critic.md is missing."""
    (tmp_path / "turma.toml").write_text(VALID_CONFIG_TEXT)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "author.md").write_text(AUTHOR_ROLE)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PlanningError, match="critic.md"):
        run_planning("some-feature")


def test_fails_when_critic_model_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """turma plan fails clearly when planning.critic_model is missing."""
    (tmp_path / "turma.toml").write_text("""\
[planning]
author_model = "claude-opus-4-6"
""")
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "author.md").write_text(AUTHOR_ROLE)
    (tmp_path / ".agents" / "critic.md").write_text(CRITIC_ROLE)
    (tmp_path / "openspec" / "changes").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    with patch("turma.planning.shutil.which", return_value="/usr/bin/mock"):
        with pytest.raises(PlanningError, match="planning.critic_model"):
            run_planning("some-feature")


@patch("turma.planning.shutil.which", return_value=None)
def test_fails_when_openspec_not_on_path(
    mock_which: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """turma plan fails when openspec CLI is not found."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PlanningError, match="openspec"):
        run_planning("some-feature")


@patch("turma.planning.shutil.which", side_effect=lambda cmd: None if cmd == "claude" else "/usr/bin/openspec")
def test_fails_when_claude_not_on_path(
    mock_which: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """turma plan fails when claude CLI is not found."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PlanningError, match="claude"):
        run_planning("some-feature")


@patch("turma.planning.shutil.which", return_value="/usr/bin/mock")
def test_fails_when_change_already_exists(
    mock_which: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """turma plan fails if the change directory already exists."""
    _setup_project(tmp_path)
    (tmp_path / "openspec" / "changes" / "existing-feature").mkdir()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(PlanningError, match="already exists"):
        run_planning("existing-feature")


@patch("turma.planning._get_backend")
@patch("turma.planning.shutil.which", return_value="/usr/bin/mock")
@patch("turma.planning.subprocess.run")
def test_calls_openspec_new_change(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_get_backend: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """turma plan calls openspec new change with the feature name."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    mock_run.side_effect = _mock_openspec("test-feat")
    backend = MagicMock()
    backend.generate.side_effect = _artifact_output_from_prompt
    mock_get_backend.return_value = backend

    run_planning("test-feat")

    new_change_call = mock_run.call_args_list[0]
    assert new_change_call[0][0] == ["openspec", "new", "change", "test-feat"]


@patch("turma.planning._get_backend")
@patch("turma.planning.shutil.which", return_value="/usr/bin/mock")
@patch("turma.planning.subprocess.run")
def test_generates_artifacts_in_order(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_get_backend: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """turma plan generates proposal, design, tasks in that order."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    mock_run.side_effect = _mock_openspec("test-feat")
    backend = MagicMock()
    backend.generate.side_effect = _artifact_output_from_prompt
    mock_get_backend.return_value = backend

    run_planning("test-feat")

    instruction_calls = [
        c for c in mock_run.call_args_list
        if c[0][0][0] == "openspec" and c[0][0][1] == "instructions"
    ]
    assert len(instruction_calls) == 3
    assert instruction_calls[0][0][0][2] == "proposal"
    assert instruction_calls[1][0][0][2] == "design"
    assert instruction_calls[2][0][0][2] == "tasks"


@patch("turma.planning._get_backend")
@patch("turma.planning.shutil.which", return_value="/usr/bin/mock")
@patch("turma.planning.subprocess.run")
def test_writes_artifacts_to_output_paths(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_get_backend: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """turma plan writes claude output to the outputPath from instructions."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    mock_run.side_effect = _mock_openspec("test-feat")
    backend = MagicMock()
    backend.generate.side_effect = _artifact_output_from_prompt
    mock_get_backend.return_value = backend

    run_planning("test-feat")

    change_dir = tmp_path / "openspec" / "changes" / "test-feat"
    assert (change_dir / PROPOSAL_INSTRUCTIONS["outputPath"]).exists()
    assert (change_dir / DESIGN_INSTRUCTIONS["outputPath"]).exists()
    assert (change_dir / TASKS_INSTRUCTIONS["outputPath"]).exists()
    assert (change_dir / "critique_1.md").exists()


@patch("turma.planning._get_backend")
@patch("turma.planning.shutil.which", return_value="/usr/bin/mock")
@patch("turma.planning.subprocess.run")
def test_prompt_includes_author_role(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_get_backend: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """turma plan includes .agents/author.md content in the prompt."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    mock_run.side_effect = _mock_openspec("test-feat")
    backend = MagicMock()
    backend.generate.side_effect = _artifact_output_from_prompt
    mock_get_backend.return_value = backend

    run_planning("test-feat")

    first_prompt = backend.generate.call_args_list[0].args[0]
    assert "draft and revise OpenSpec artifacts" in first_prompt


@patch("turma.planning._get_backend")
@patch("turma.planning.shutil.which", return_value="/usr/bin/mock")
@patch("turma.planning.subprocess.run")
def test_prompt_includes_openspec_instructions(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_get_backend: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """turma plan includes template and instruction from openspec JSON."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    mock_run.side_effect = _mock_openspec("test-feat")
    backend = MagicMock()
    backend.generate.side_effect = _artifact_output_from_prompt
    mock_get_backend.return_value = backend

    run_planning("test-feat")

    first_prompt = backend.generate.call_args_list[0].args[0]
    assert "Create the proposal document" in first_prompt
    assert "## Why" in first_prompt


@patch("turma.planning._get_backend")
@patch("turma.planning.shutil.which", return_value="/usr/bin/mock")
@patch("turma.planning.subprocess.run")
def test_design_prompt_includes_proposal_dependency(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_get_backend: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Design prompt includes proposal content as a dependency."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    mock_run.side_effect = _mock_openspec("test-feat")
    backend = MagicMock()
    backend.generate.side_effect = _artifact_output_from_prompt
    mock_get_backend.return_value = backend

    run_planning("test-feat")

    design_prompt = backend.generate.call_args_list[1].args[0]
    assert "<proposal>\n## Why\nText" in design_prompt


@patch("turma.planning._get_backend")
@patch("turma.planning.shutil.which", return_value="/usr/bin/mock")
@patch("turma.planning.subprocess.run")
def test_openspec_failure_raises_planning_error(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_get_backend: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero OpenSpec exit raises PlanningError with stderr."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    backend = MagicMock()
    mock_get_backend.return_value = backend

    failed = MagicMock()
    failed.returncode = 1
    failed.stderr = "something went wrong"
    mock_run.return_value = failed

    with pytest.raises(PlanningError, match="scaffolding change test-feat failed"):
        run_planning("test-feat")


@patch("turma.planning._get_backend")
@patch("turma.planning.shutil.which", return_value="/usr/bin/mock")
@patch("turma.planning.subprocess.run")
def test_backend_failure_reports_artifact_step(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_get_backend: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend failures identify the artifact step that failed."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    mock_run.side_effect = _mock_openspec("test-feat")

    backend = MagicMock()
    backend.generate.side_effect = PlanningError("generating proposal failed: backend exploded")
    mock_get_backend.return_value = backend

    with pytest.raises(PlanningError, match="generating proposal failed"):
        run_planning("test-feat")


@patch("turma.planning._get_backend")
@patch("turma.planning.shutil.which", return_value="/usr/bin/mock")
@patch("turma.planning.subprocess.run")
def test_uses_author_model_from_config(
    mock_run: MagicMock,
    mock_which: MagicMock,
    mock_get_backend: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """turma plan passes the author_model from config to the backend."""
    _setup_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    mock_run.side_effect = _mock_openspec("test-feat")
    backend = MagicMock()
    backend.generate.side_effect = _artifact_output_from_prompt
    mock_get_backend.return_value = backend

    run_planning("test-feat")

    assert backend.generate.call_args_list[0].args[1] == "claude-opus-4-6"


@patch("turma.planning.ClaudeAuthorBackend")
def test_get_backend_selects_claude_for_claude_models(
    mock_backend_cls: MagicMock,
) -> None:
    """Claude models select the Claude backend."""
    backend = MagicMock()
    mock_backend_cls.return_value = backend

    selected = _get_backend("claude-opus-4-6")

    assert selected is backend
    mock_backend_cls.assert_called_once_with()


@patch("turma.planning.CodexAuthorBackend")
def test_get_backend_selects_codex_for_gpt_models(
    mock_backend_cls: MagicMock,
) -> None:
    """OpenAI/Codex models select the Codex backend."""
    backend = MagicMock()
    mock_backend_cls.return_value = backend

    selected = _get_backend("gpt-5.4")

    assert selected is backend
    mock_backend_cls.assert_called_once_with()


@patch("turma.planning.CodexAuthorBackend")
def test_get_backend_selects_codex_for_o_series_models(
    mock_backend_cls: MagicMock,
) -> None:
    """o-series models select the Codex backend."""
    backend = MagicMock()
    mock_backend_cls.return_value = backend

    selected = _get_backend("o3")

    assert selected is backend
    mock_backend_cls.assert_called_once_with()


@patch("turma.planning.OpenCodeAuthorBackend")
def test_get_backend_selects_opencode_for_provider_model_format(
    mock_backend_cls: MagicMock,
) -> None:
    """provider/model format selects the OpenCode backend."""
    backend = MagicMock()
    mock_backend_cls.return_value = backend

    selected = _get_backend("groq/llama-3.3-70b-versatile")

    assert selected is backend
    mock_backend_cls.assert_called_once_with()


@patch("turma.planning.OpenCodeAuthorBackend")
def test_get_backend_selects_opencode_for_anthropic_provider_format(
    mock_backend_cls: MagicMock,
) -> None:
    """anthropic/claude-* routes to OpenCode, not Claude CLI."""
    backend = MagicMock()
    mock_backend_cls.return_value = backend

    selected = _get_backend("anthropic/claude-sonnet-4-6")

    assert selected is backend
    mock_backend_cls.assert_called_once_with()


@patch("turma.planning.GeminiAuthorBackend")
def test_get_backend_selects_gemini_for_gemini_models(
    mock_backend_cls: MagicMock,
) -> None:
    """Gemini models select the Gemini backend."""
    backend = MagicMock()
    mock_backend_cls.return_value = backend

    selected = _get_backend("gemini-2.5-flash")

    assert selected is backend
    mock_backend_cls.assert_called_once_with()


def test_get_backend_rejects_unknown_model_prefix() -> None:
    """Unknown planning models fail clearly until more backends are added."""
    with pytest.raises(PlanningError, match="unsupported planning author model"):
        _get_backend("llama-3")


def test_prompt_tells_author_to_make_reasonable_assumptions() -> None:
    """Prompt explicitly forbids clarification requests and asks for assumptions."""
    prompt = _build_prompt(
        author_role=AUTHOR_ROLE,
        instructions=PROPOSAL_INSTRUCTIONS,
        dep_content="",
        feature="test-feat",
    )

    assert "Do not ask clarifying questions" in prompt
    assert "make the most reasonable implementation-oriented assumptions" in prompt


def test_prompt_tells_author_to_stay_grounded_in_requested_feature() -> None:
    """Prompt explicitly forbids unrelated product reinvention."""
    prompt = _build_prompt(
        author_role=AUTHOR_ROLE,
        instructions=PROPOSAL_INSTRUCTIONS,
        dep_content="",
        feature="turma-plan-opencode",
    )

    assert "Stay grounded in the requested feature and the existing Turma codebase" in prompt
    assert "Do not invent unrelated product changes" in prompt


def test_prompt_tells_author_to_follow_existing_backend_pattern() -> None:
    """Prompt explicitly anchors backend work to the existing authoring files."""
    prompt = _build_prompt(
        author_role=AUTHOR_ROLE,
        instructions=PROPOSAL_INSTRUCTIONS,
        dep_content="",
        feature="add-opencode-planning-backend",
    )

    assert "Mirror the existing backend pattern in src/turma/authoring/" in prompt
    assert "follow that file shape and the existing routing in src/turma/planning.py" in prompt
    assert "Do not propose Beads integration" in prompt


def test_build_repo_context_includes_source_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repo context includes Python source file paths."""
    (tmp_path / "src" / "turma" / "authoring").mkdir(parents=True)
    (tmp_path / "src" / "turma" / "cli.py").write_text("")
    (tmp_path / "src" / "turma" / "authoring" / "claude.py").write_text("")
    (tmp_path / "README.md").write_text("# Turma\nA Python CLI.\n")
    monkeypatch.chdir(tmp_path)

    ctx = _build_repo_context()

    assert "Python CLI" in ctx
    assert "src/turma/cli.py" in ctx
    assert "src/turma/authoring/claude.py" in ctx
    assert "No JavaScript" in ctx


def test_build_prompt_includes_repo_context() -> None:
    """Prompt includes repo context when provided."""
    prompt = _build_prompt(
        author_role=AUTHOR_ROLE,
        instructions=PROPOSAL_INSTRUCTIONS,
        dep_content="",
        feature="test-feat",
        repo_context="This is a Python CLI. No JavaScript.",
    )

    assert "<repo-context>" in prompt
    assert "This is a Python CLI" in prompt


def test_extract_instructions_json_accepts_plain_json() -> None:
    """JSON parsing works when openspec emits only JSON."""
    raw = json.dumps(PROPOSAL_INSTRUCTIONS)

    parsed = _extract_instructions_json(raw, "proposal")

    assert parsed["artifactId"] == "proposal"


def test_extract_instructions_json_accepts_multiple_preamble_lines() -> None:
    """JSON parsing tolerates more than one non-JSON preamble line."""
    raw = (
        "- Generating instructions...\n"
        "- Validating change state...\n"
        f"{json.dumps(PROPOSAL_INSTRUCTIONS)}"
    )

    parsed = _extract_instructions_json(raw, "proposal")

    assert parsed["artifactId"] == "proposal"


def test_extract_instructions_json_fails_when_no_json_present() -> None:
    """JSON parsing fails clearly when stdout contains no JSON payload."""
    with pytest.raises(PlanningError, match="no JSON object found"):
        _extract_instructions_json("not json at all", "proposal")


def test_validate_artifact_output_rejects_empty_output() -> None:
    """Empty model output fails clearly instead of writing a blank artifact."""
    with pytest.raises(PlanningError, match="empty output"):
        _validate_artifact_output("", "proposal", PROPOSAL_INSTRUCTIONS["template"], "test-feat")


def test_validate_artifact_output_rejects_clarification_request() -> None:
    """Clarification questions are rejected as invalid artifact output."""
    with pytest.raises(PlanningError, match="asked for clarification"):
        _validate_artifact_output(
            "I need to understand the scope before writing this. Could you clarify?",
            "proposal",
            PROPOSAL_INSTRUCTIONS["template"],
            "test-feat",
        )


def test_validate_artifact_output_strips_preamble_and_normalizes() -> None:
    """Leading conversational preamble is removed before writing the artifact."""
    raw = (
        'Now I have everything I need. Let me write the proposal for "test-feat".\n\n'
        "## Why\nText\n\n## What Changes\nStuff"
    )
    assert _validate_artifact_output(
        raw,
        "proposal",
        PROPOSAL_INSTRUCTIONS["template"],
        "test-feat",
    ) == "## Why\nText\n\n## What Changes\nStuff\n"


def test_validate_artifact_output_rejects_missing_template_headings() -> None:
    """Artifact output must contain all required headings from the template."""
    with pytest.raises(PlanningError, match="missing required template headings"):
        _validate_artifact_output(
            "## Why\nText",
            "proposal",
            PROPOSAL_INSTRUCTIONS["template"],
            "test-feat",
        )


def test_strip_leading_preamble_keeps_first_heading_block() -> None:
    """Preamble stripping starts output at the first markdown heading."""
    assert _strip_leading_preamble("hello\n\n## Why\nText") == "## Why\nText"


def test_strip_wrapping_code_fence_removes_outer_fence() -> None:
    """A single full-output markdown fence is stripped before validation."""
    assert _strip_wrapping_code_fence("```markdown\n## Why\nText\n```") == "## Why\nText"


def test_validate_artifact_output_accepts_wrapped_markdown_artifact() -> None:
    """Wrapped markdown artifacts are normalized instead of rejected."""
    assert _validate_artifact_output(
        "```markdown\n## Why\nText\n\n## What Changes\nStuff\n```",
        "proposal",
        PROPOSAL_INSTRUCTIONS["template"],
        "test-feat",
    ) == "## Why\nText\n\n## What Changes\nStuff\n"


def test_validate_artifact_output_rejects_off_target_backend_boilerplate() -> None:
    """Backend/provider features reject generic product-planning drift."""
    with pytest.raises(PlanningError, match="drifted into unrelated product-planning content"):
        _validate_artifact_output(
            (
                "## Why\n"
                "This change adds AI-driven coding assistance.\n\n"
                "## What Changes\n"
                "Use a microservices-based architecture and update the user interface.\n"
            ),
            "proposal",
            PROPOSAL_INSTRUCTIONS["template"],
            "turma-plan-opencode",
        )


def test_validate_artifact_output_rejects_invented_backend_module() -> None:
    """Backend features should not invent new authoring module names."""
    with pytest.raises(PlanningError, match="invented a backend module"):
        _validate_artifact_output(
            (
                "## Why\n"
                "Need OpenCode planning support.\n\n"
                "## What Changes\n"
                "Implement src/turma/authoring/opencode_planning.py to add the backend.\n"
            ),
            "proposal",
            PROPOSAL_INSTRUCTIONS["template"],
            "add-opencode-planning-backend",
        )


def test_validate_artifact_output_rejects_beads_for_backend_feature() -> None:
    """Backend/provider features should reject Beads drift."""
    with pytest.raises(PlanningError, match="beads task tracking system"):
        _validate_artifact_output(
            (
                "## Why\n"
                "Need OpenCode planning support.\n\n"
                "## What Changes\n"
                "Integrate the backend with the Beads task tracking system.\n"
            ),
            "proposal",
            PROPOSAL_INSTRUCTIONS["template"],
            "add-opencode-planning-backend",
        )


def test_validate_artifact_output_accepts_known_gemini_backend_path() -> None:
    """Artifacts referencing the gemini backend path should not be rejected."""
    result = _validate_artifact_output(
        (
            "## Why\n"
            "Need Gemini planning support.\n\n"
            "## What Changes\n"
            "Add src/turma/authoring/gemini.py as the Gemini backend.\n"
        ),
        "proposal",
        PROPOSAL_INSTRUCTIONS["template"],
        "add-gemini-backend",
    )
    assert "src/turma/authoring/gemini.py" in result


def test_extract_template_headings_reads_markdown_headings() -> None:
    """Template headings are extracted for validation."""
    assert _extract_template_headings("## Why\n\n## What Changes\n") == [
        "## Why",
        "## What Changes",
    ]


def test_extract_template_headings_skips_placeholder_headings() -> None:
    """Placeholder headings with HTML comments are not treated as literal requirements."""
    template = "## 1. <!-- Task Group Name -->\n\n## Real Heading\n"
    assert _extract_template_headings(template) == ["## Real Heading"]
