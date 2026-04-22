"""Planning orchestration for the Turma CLI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from turma.authoring.base import AuthorBackend
from turma.authoring.claude import ClaudeAuthorBackend
from turma.authoring.codex import CodexAuthorBackend
from turma.authoring.gemini import GeminiAuthorBackend
from turma.authoring.opencode import OpenCodeAuthorBackend
from turma.config import ConfigError, load_config
from turma.errors import PlanningError
from turma.planning.critique_parser import ParseFailure, ParsedCritique, ParseResult, parse_critique

ARTIFACT_ORDER = ["proposal", "design", "tasks"]
# Terminal markers written by the state machine / resume commands.
# Mirrored in state_machine.reconcile_current_state; kept local here to avoid
# a circular import between planning/__init__.py and planning/state_machine.py.
_TERMINAL_MARKER_FILES = ("APPROVED", "ABANDONED.md", "NEEDS_HUMAN_REVIEW.md")


def _has_terminal_marker(change_dir: Path) -> bool:
    """True if the change directory contains any terminal-state marker."""
    return any((change_dir / name).exists() for name in _TERMINAL_MARKER_FILES)

QUESTION_PATTERNS = (
    "could you clarify",
    "can you clarify",
    "i need to understand",
    "i don't have context",
    "my best guess",
    "which direction should i take",
)
BACKEND_FEATURE_TOKENS = {"backend", "provider", "opencode", "codex", "claude", "gemini"}
OFF_TARGET_BACKEND_PATTERNS = (
    "microservices-based architecture",
    "ai-driven coding assistance",
    "project management capabilities",
    "roll out the integration to users",
    "update the user interface",
    "beads task tracking system",
    "package.json",
    "api integration",
    "rest api",
    "deploy the changes",
    "monitor performance",
    "production environment",
    "rollout plan",
)


@dataclass
class PlanningRoles:
    """Role prompts used by planning agents."""

    author: str
    critic: str


@dataclass
class PlanningServices:
    """Injectable service boundary for planning orchestration."""

    get_backend: Callable[[str], AuthorBackend]
    run_openspec: Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass
class PlanningSession:
    """Loaded planning context for a single feature."""

    feature: str
    change_dir: Path
    author_model: str
    critic_model: str
    author_backend: AuthorBackend
    critic_backend: AuthorBackend
    roles: PlanningRoles
    services: PlanningServices
    max_rounds: int = 4
    interactive: bool = True


def default_planning_services() -> PlanningServices:
    """Return production planning services."""
    return PlanningServices(
        get_backend=_get_backend,
        run_openspec=lambda cmd: _run_openspec(
            cmd,
            step=_openspec_step_name(cmd),
        ),
    )


def run_planning(
    feature: str,
    services: PlanningServices | None = None,
) -> None:
    """Run planning through the critic-loop state machine."""
    session = _prepare_planning_session(
        feature,
        services or default_planning_services(),
    )

    print("loading config from turma.toml")
    print(f"author model: {session.author_model}")
    print(f"creating change: {feature}")

    from turma.planning.state_machine import run_planning_state_machine

    result = run_planning_state_machine(session)
    if result.next_nodes:
        print(f"planning suspended before: {', '.join(result.next_nodes)}")
    print(f"checkpoint: {result.checkpoint_path}")

    round_num = int(result.state.get("round", 1))
    current_state = result.state.get("state")

    if result.next_nodes:
        print(
            f"\nplanning paused at round {round_num}. "
            f"review openspec/changes/{feature}/critique_{round_num}.md "
            "then resume with:"
        )
        _print_resume_command_hints(feature, include_override=False)
    elif current_state == "needs_human_review":
        print(
            f"\nplanning halted at needs_human_review. review "
            f"openspec/changes/{feature}/NEEDS_HUMAN_REVIEW.md then override with:"
        )
        _print_resume_command_hints(feature, include_override=True)
    elif current_state == "abandoned":
        print(
            f"\nplanning abandoned. see openspec/changes/{feature}/ABANDONED.md"
        )
    else:
        print(f"\nplanning complete. artifacts written to openspec/changes/{feature}/")


def _print_resume_command_hints(feature: str, *, include_override: bool) -> None:
    """Print the resume command surface for a suspended or halted plan."""
    if include_override:
        print(
            f'  turma plan --feature {feature} --resume --approve '
            f'--override "<reason>"'
        )
        return
    print(f"  turma plan --feature {feature} --resume --approve")
    print(f'  turma plan --feature {feature} --resume --revise "<reason>"')
    print(f'  turma plan --feature {feature} --resume --abandon "<reason>"')


def _prepare_planning_session(
    feature: str,
    services: PlanningServices,
    *,
    require_fresh: bool = True,
) -> PlanningSession:
    """Load config, roles, and validate the initial planning target.

    When ``require_fresh`` is True (default, used by ``turma plan``), the
    change directory must not already exist and the openspec CLI must be
    on PATH. When False (used by the resume flow), the change directory
    must already exist and the openspec CLI is not required.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        raise PlanningError(str(exc)) from exc

    author_model = config.planning.author_model
    critic_model = config.planning.critic_model

    change_dir = Path.cwd() / "openspec" / "changes" / feature
    if require_fresh and change_dir.exists() and not _has_terminal_marker(change_dir):
        raise PlanningError(
            f"openspec/changes/{feature}/ already exists. "
            "Remove it or pick a different feature name."
        )
    if not require_fresh and not change_dir.exists():
        raise PlanningError(
            f"openspec/changes/{feature}/ does not exist. "
            "Run turma plan first before resuming."
        )

    roles = _load_planning_roles()
    if require_fresh and shutil.which("openspec") is None:
        raise PlanningError(
            "openspec CLI not found. Install it: npm install -g @fission-ai/openspec"
        )
    if not critic_model:
        raise PlanningError(
            "planning.critic_model is required in turma.toml for critic review"
        )
    author_backend = services.get_backend(author_model)
    critic_backend = services.get_backend(critic_model)

    return PlanningSession(
        feature=feature,
        change_dir=change_dir,
        author_model=author_model,
        max_rounds=config.planning.max_rounds,
        interactive=config.planning.interactive,
        critic_model=critic_model,
        author_backend=author_backend,
        critic_backend=critic_backend,
        roles=roles,
        services=services,
    )


def _load_planning_roles() -> PlanningRoles:
    """Load planning role prompts from the current repository."""
    author_path = Path.cwd() / ".agents" / "author.md"
    critic_path = Path.cwd() / ".agents" / "critic.md"
    if not author_path.exists():
        raise PlanningError(
            ".agents/author.md not found. Create it before running turma plan."
        )
    if not critic_path.exists():
        raise PlanningError(
            ".agents/critic.md not found. Create it before running turma plan."
        )
    return PlanningRoles(
        author=author_path.read_text(),
        critic=critic_path.read_text(),
    )


def _scaffold_change(session: PlanningSession) -> None:
    """Create the OpenSpec change directory for a planning session."""
    session.services.run_openspec(
        ["openspec", "new", "change", session.feature],
    )


def _generate_initial_artifacts(session: PlanningSession) -> dict[str, Path]:
    """Generate the initial proposal/design/tasks artifacts in order."""
    written_artifacts: dict[str, Path] = {}

    for artifact_id in ARTIFACT_ORDER:
        print(f"generating {artifact_id} (this may take 1-2 min) ...", end=" ", flush=True)

        instructions = _get_instructions(artifact_id, session)
        output_path = session.change_dir / instructions["outputPath"]
        dep_content = _read_dependencies(instructions, written_artifacts)
        prompt = _build_prompt(
            author_role=session.roles.author,
            instructions=instructions,
            dep_content=dep_content,
            feature=session.feature,
            repo_context=_build_repo_context(),
        )

        raw_output = session.author_backend.generate(
            prompt,
            session.author_model,
            timeout=300,
        )
        artifact_text = _validate_artifact_output(
            raw_output,
            artifact_id,
            instructions.get("template", ""),
            session.feature,
        )
        _write_artifact(output_path, artifact_text)
        written_artifacts[artifact_id] = output_path

        print("done")

    return written_artifacts


def _run_critic_review(
    session: PlanningSession,
    written_artifacts: dict[str, Path],
    round_num: int = 1,
) -> ParseResult:
    """Run the critic review for a given round and write critique_N.md."""
    prompt = _build_critic_prompt(
        critic_role=session.roles.critic,
        feature=session.feature,
        artifact_content=_read_artifact_set(written_artifacts),
        round_num=round_num,
        repo_context=_build_repo_context(),
    )
    raw_output = session.critic_backend.generate(
        prompt,
        session.critic_model,
        timeout=300,
    )
    critique_text = _normalize_generated_markdown(raw_output)
    critique_path = session.change_dir / f"critique_{round_num}.md"
    _write_artifact(critique_path, critique_text)
    return parse_critique(critique_text)


# Back-compat alias: pre-existing callers expect the name
# `_run_initial_critic_review`. Delegates to the generalized round-aware
# implementation.
_run_initial_critic_review = _run_critic_review


def _print_critic_result(result: ParseResult) -> None:
    """Print the first critic route without advancing state."""
    if isinstance(result, ParsedCritique):
        print(f"critic status: {result.status.value}")
    else:
        print(f"critic parse failure: {result.reason}")
    print(f"critic route: {result.route.value}")


def _read_artifact_set(written_artifacts: dict[str, Path]) -> str:
    """Read generated planning artifacts for critic input."""
    parts = []
    for artifact_id in ARTIFACT_ORDER:
        path = written_artifacts.get(artifact_id)
        if path is None:
            continue
        parts.append(f"<{artifact_id}>\n{path.read_text()}\n</{artifact_id}>")
    return "\n\n".join(parts)


def _generate_round_revision(
    session: PlanningSession,
    round_num: int,
) -> dict[str, Path]:
    """Run the two-call revision path for a round N >= 2 and return artifact paths.

    First call generates ``response_{N-1}.md`` from ``critique_{N-1}.md`` plus
    the prior-round artifact set (and any human revision reason recorded in
    ``response_{N-1}_human.md``). Second call regenerates proposal/design/
    tasks using the response, human reason, and prior artifacts as context.

    Partial-failure rule: if the response file already exists on disk (from a
    prior crashed attempt), it is reused verbatim and only the revised-draft
    call runs. Response is written before revised drafts so recovery from a
    mid-revision crash always has the response artifact to replay against.
    """
    if round_num < 2:
        raise PlanningError(
            f"_generate_round_revision requires round >= 2 (got {round_num})"
        )

    prev_round = round_num - 1
    critique_path = session.change_dir / f"critique_{prev_round}.md"
    if not critique_path.exists():
        raise PlanningError(
            f"cannot run round {round_num}: {critique_path.name} not found"
        )
    critique_text = critique_path.read_text()

    prior_artifacts = {
        artifact_id: session.change_dir / f"{artifact_id}.md"
        for artifact_id in ARTIFACT_ORDER
    }
    prior_artifact_content = _read_artifact_set(prior_artifacts)

    human_reason_path = session.change_dir / f"response_{prev_round}_human.md"
    human_reason = (
        human_reason_path.read_text() if human_reason_path.exists() else ""
    )

    response_path = session.change_dir / f"response_{prev_round}.md"
    if response_path.exists():
        response_text = response_path.read_text()
    else:
        response_text = _generate_response(
            session,
            prev_round=prev_round,
            critique_text=critique_text,
            prior_artifact_content=prior_artifact_content,
            human_reason=human_reason,
        )
        _write_artifact(response_path, response_text)

    return _generate_revised_artifacts(
        session,
        round_num=round_num,
        prev_round=prev_round,
        critique_text=critique_text,
        response_text=response_text,
        prior_artifact_content=prior_artifact_content,
        human_reason=human_reason,
    )


def _generate_response(
    session: PlanningSession,
    prev_round: int,
    critique_text: str,
    prior_artifact_content: str,
    human_reason: str = "",
) -> str:
    """Author call 1: produce the per-finding response to a prior critique."""
    print(
        f"generating response_{prev_round}.md (this may take 1-2 min) ...",
        end=" ",
        flush=True,
    )
    prompt = _build_response_prompt(
        author_role=session.roles.author,
        feature=session.feature,
        prev_round=prev_round,
        critique_text=critique_text,
        prior_artifact_content=prior_artifact_content,
        human_reason=human_reason,
        repo_context=_build_repo_context(),
    )
    raw_output = session.author_backend.generate(
        prompt,
        session.author_model,
        timeout=300,
    )
    response_text = _normalize_generated_markdown(raw_output)
    print("done")
    return response_text


def _build_response_prompt(
    author_role: str,
    feature: str,
    prev_round: int,
    critique_text: str,
    prior_artifact_content: str,
    human_reason: str = "",
    repo_context: str = "",
) -> str:
    """Assemble the author prompt for the per-finding response artifact."""
    parts = [
        f"You are an author agent. Your role:\n\n<role>\n{author_role}\n</role>",
        (
            f'You are generating the per-finding response artifact for change '
            f'"{feature}". You must emit response_{prev_round}.md responding '
            f"to critique_{prev_round}.md BEFORE any revised draft is written."
        ),
    ]

    if repo_context:
        parts.append(f"<repo-context>\n{repo_context}\n</repo-context>")

    parts.append(f"<critique>\n{critique_text}</critique>")
    parts.append(f"<artifacts>\n{prior_artifact_content}\n</artifacts>")

    if human_reason.strip():
        parts.append(
            f"<human-revision-reason>\n{human_reason}\n</human-revision-reason>"
        )
        parts.append(
            "The <human-revision-reason> block is operator-supplied "
            "context that justifies why a revision was requested after the "
            "prior round. Treat it as authoritative intent: any finding it "
            "implicitly supports should be Accepted, and the revision must "
            "address it directly even if the critic did not emphasize it."
        )

    parts.append(
        "For each finding ID in the critique, write a single markdown section "
        "with an explicit Accept or Reject decision and a short rationale. "
        "Use this exact shape:\n\n"
        "## [B001] Accept — one-line rationale\n\n"
        "Keep one section per finding ID. Do not revise the spec artifacts "
        "in this output — revision is a separate call."
    )
    parts.append(
        "Output ONLY the response markdown. No preamble, no commentary, "
        "no code fences."
    )
    return "\n\n".join(parts)


def _generate_revised_artifacts(
    session: PlanningSession,
    round_num: int,
    prev_round: int,
    critique_text: str,
    response_text: str,
    prior_artifact_content: str,
    human_reason: str = "",
) -> dict[str, Path]:
    """Author call 2: regenerate the three planning artifacts using the response."""
    written_artifacts: dict[str, Path] = {}
    backend = session.author_backend

    for artifact_id in ARTIFACT_ORDER:
        print(
            f"revising {artifact_id} for round {round_num} (this may take 1-2 min) ...",
            end=" ",
            flush=True,
        )

        instructions = _get_instructions(artifact_id, session)
        output_path = session.change_dir / instructions["outputPath"]
        dep_content = _read_dependencies(instructions, written_artifacts)
        prompt = _build_revision_prompt(
            author_role=session.roles.author,
            instructions=instructions,
            dep_content=dep_content,
            feature=session.feature,
            round_num=round_num,
            prev_round=prev_round,
            critique_text=critique_text,
            response_text=response_text,
            prior_artifact_content=prior_artifact_content,
            human_reason=human_reason,
            repo_context=_build_repo_context(),
        )

        raw_output = backend.generate(prompt, session.author_model, timeout=300)
        artifact_text = _validate_artifact_output(
            raw_output,
            artifact_id,
            instructions.get("template", ""),
            session.feature,
        )
        _write_artifact(output_path, artifact_text)
        written_artifacts[artifact_id] = output_path

        print("done")

    return written_artifacts


def _build_revision_prompt(
    author_role: str,
    instructions: dict,
    dep_content: str,
    feature: str,
    round_num: int,
    prev_round: int,
    critique_text: str,
    response_text: str,
    prior_artifact_content: str,
    human_reason: str = "",
    repo_context: str = "",
) -> str:
    """Assemble the author prompt for a revised artifact in round N >= 2."""
    artifact_id = instructions["artifactId"]
    instruction = instructions.get("instruction", "")
    template = instructions.get("template", "")
    context = instructions.get("context", "")
    rules = instructions.get("rules", "")

    parts = [
        f"You are an author agent. Your role:\n\n<role>\n{author_role}\n</role>",
        (
            f'You are revising the "{artifact_id}" artifact for change '
            f'"{feature}" in round {round_num}. Apply the Accepted findings '
            f"from response_{prev_round}.md and do not regress any previously "
            "correct content. Rejected findings may be left unchanged."
        ),
    ]

    if repo_context:
        parts.append(f"<repo-context>\n{repo_context}\n</repo-context>")

    parts.extend([
        f"<instructions>\n{instruction}\n</instructions>",
        f"<template>\n{template}\n</template>",
    ])

    if context:
        parts.append(f"<context>\n{context}\n</context>")

    if rules:
        parts.append(f"<rules>\n{rules}\n</rules>")

    parts.append(f"<critique>\n{critique_text}</critique>")
    parts.append(f"<response>\n{response_text}</response>")
    parts.append(
        f"<prior-artifacts>\n{prior_artifact_content}\n</prior-artifacts>"
    )

    if human_reason.strip():
        parts.append(
            f"<human-revision-reason>\n{human_reason}\n</human-revision-reason>"
        )
        parts.append(
            "The revised artifact must directly address the concerns in "
            "<human-revision-reason>, which is operator-supplied intent from "
            "the prior human approval gate."
        )

    if dep_content:
        parts.append(
            f"<revised-dependencies>\n{dep_content}\n</revised-dependencies>"
        )

    parts.append(
        "Output ONLY the revised artifact markdown. No preamble, no commentary, "
        "no code fences."
    )
    parts.append(
        "Keep the same document shape as the prior artifact. Incorporate "
        "Accepted findings; do not introduce unrelated changes."
    )
    return "\n\n".join(parts)


def _build_critic_prompt(
    critic_role: str,
    feature: str,
    artifact_content: str,
    round_num: int = 1,
    repo_context: str = "",
) -> str:
    """Assemble the critic prompt for a completed author round."""
    parts = [
        f"You are a critic agent. Your role:\n\n<role>\n{critic_role}\n</role>",
        f'Review the round {round_num} planning artifacts for change "{feature}".',
    ]

    if repo_context:
        parts.append(f"<repo-context>\n{repo_context}\n</repo-context>")

    parts.append(f"<artifacts>\n{artifact_content}\n</artifacts>")
    parts.append(
        "Write a strict machine-readable critique. Use exactly one status line: "
        "## Status: blocking, ## Status: nits_only, or ## Status: approved."
    )
    parts.append(
        "Under ## Findings, write finding lines in this exact form: "
        "- [B001] [blocking] [design.md] Message. "
        "Use B IDs for blocking issues, N IDs for nits, and Q IDs for questions."
    )
    parts.append(
        "Status is authoritative for routing. Use blocking only when the author "
        "must revise before human approval; use nits_only for non-blocking issues; "
        "use approved only when no changes are needed."
    )
    parts.append(
        "Output ONLY the critique markdown. No preamble, no commentary, no code fences."
    )
    return "\n\n".join(parts)


def _normalize_generated_markdown(raw: str) -> str:
    """Normalize generated markdown for file output."""
    text = _strip_leading_preamble(_strip_wrapping_code_fence(raw.strip()))
    if not text:
        raise PlanningError("generating critique failed: critic returned empty output")
    return text + "\n"


def _write_artifact(path: Path, content: str) -> None:
    """Write generated artifact content to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _get_backend(model: str) -> AuthorBackend:
    """Return the author backend for the configured model."""
    if "/" in model:
        return OpenCodeAuthorBackend()
    if model.startswith("claude-"):
        return ClaudeAuthorBackend()
    if (
        model.startswith("gpt-")
        or model.startswith("codex-")
        or re.match(r"^o\d", model)
    ):
        return CodexAuthorBackend()
    if model.startswith("gemini-"):
        return GeminiAuthorBackend()
    raise PlanningError(
        f"unsupported planning author model: {model}. "
        "Supported prefixes: claude-*, gpt-*, codex-*, o*, gemini-*, or provider/model format."
    )


def _run_openspec(
    cmd: Sequence[str],
    *,
    step: str,
) -> subprocess.CompletedProcess[str]:
    """Run an OpenSpec subprocess and raise PlanningError on failure."""
    result = subprocess.run(list(cmd), capture_output=True, text=True)
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or "unknown error"
        )
        raise PlanningError(
            f"{step} failed: {cmd[0]} exited with {result.returncode}\n{detail}"
        )
    return result


def _openspec_step_name(cmd: Sequence[str]) -> str:
    """Return a human-readable step name for an OpenSpec command."""
    if len(cmd) >= 4 and cmd[:3] == ["openspec", "new", "change"]:
        return f"scaffolding change {cmd[3]}"
    if len(cmd) >= 3 and cmd[:2] == ["openspec", "instructions"]:
        return f"loading instructions for {cmd[2]}"
    return "running openspec"


def _get_instructions(artifact_id: str, session: PlanningSession) -> dict:
    """Get openspec instructions JSON for an artifact."""
    result = session.services.run_openspec(
        [
            "openspec", "instructions", artifact_id,
            "--change", session.feature, "--json",
        ]
    )

    return _extract_instructions_json(result.stdout, artifact_id)


def _extract_instructions_json(raw: str, artifact_id: str) -> dict:
    """Extract the JSON payload from openspec instructions output."""
    start = raw.find("{")
    if start == -1:
        raise PlanningError(
            f"failed to parse openspec instructions for {artifact_id}: "
            "no JSON object found"
        )

    json_text = raw[start:]

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as exc:
        preview = raw.strip()[:200]
        raise PlanningError(
            f"failed to parse openspec instructions for {artifact_id}: {exc}\n"
            f"raw output: {preview}"
        ) from exc


def _build_repo_context() -> str:
    """Build a compact summary of the repository for prompt grounding."""
    cwd = Path.cwd()
    lines = [
        "This is the Turma repository — a Python CLI tool.",
        "Language: Python. Build tool: setuptools + uv. No JavaScript, no package.json.",
        "",
        "Source layout:",
    ]

    src_dir = cwd / "src" / "turma"
    if src_dir.exists():
        for p in sorted(src_dir.rglob("*.py")):
            if "__pycache__" in str(p):
                continue
            rel = p.relative_to(cwd)
            lines.append(f"  {rel}")

    tests_dir = cwd / "tests"
    if tests_dir.exists():
        lines.append("")
        lines.append("Test files:")
        for p in sorted(tests_dir.glob("test_*.py")):
            rel = p.relative_to(cwd)
            lines.append(f"  {rel}")

    # Include a compact sample of an existing backend to show the pattern
    authoring_dir = src_dir / "authoring" if src_dir.exists() else None
    if authoring_dir and authoring_dir.exists():
        for backend_file in sorted(authoring_dir.glob("*.py")):
            if backend_file.name in ("__init__.py", "base.py"):
                continue
            sample_lines = backend_file.read_text().splitlines()[:25]
            lines.append("")
            lines.append(f"Existing backend pattern ({backend_file.name}):")
            for line in sample_lines:
                lines.append(f"  {line}")
            break  # one example is enough

    config = cwd / "turma.example.toml"
    if config.exists():
        lines.append("")
        lines.append("Config template: turma.example.toml")

    readme = cwd / "README.md"
    if readme.exists():
        first_lines = readme.read_text().splitlines()[:5]
        lines.append("")
        lines.append("README excerpt:")
        for line in first_lines:
            lines.append(f"  {line}")

    return "\n".join(lines)


def _read_dependencies(
    instructions: dict,
    written_artifacts: dict[str, Path],
) -> str:
    """Read content of completed dependency artifacts."""
    parts = []
    for dep in instructions.get("dependencies", []):
        dep_id = dep["id"]
        if dep_id in written_artifacts:
            content = written_artifacts[dep_id].read_text()
            parts.append(f"<{dep_id}>\n{content}\n</{dep_id}>")
    return "\n\n".join(parts)


def _build_prompt(
    author_role: str,
    instructions: dict,
    dep_content: str,
    feature: str,
    repo_context: str = "",
) -> str:
    """Assemble the author prompt for a single artifact."""
    artifact_id = instructions["artifactId"]
    instruction = instructions.get("instruction", "")
    template = instructions.get("template", "")
    context = instructions.get("context", "")
    rules = instructions.get("rules", "")

    parts = [
        f"You are an author agent. Your role:\n\n<role>\n{author_role}\n</role>",
        f'You are creating the "{artifact_id}" artifact for change "{feature}".',
    ]

    if repo_context:
        parts.append(f"<repo-context>\n{repo_context}\n</repo-context>")

    parts.extend([
        f"<instructions>\n{instruction}\n</instructions>",
        f"<template>\n{template}\n</template>",
    ])

    if context:
        parts.append(f"<context>\n{context}\n</context>")

    if rules:
        parts.append(f"<rules>\n{rules}\n</rules>")

    if dep_content:
        parts.append(f"<dependencies>\n{dep_content}\n</dependencies>")

    parts.append(
        "Output ONLY the artifact markdown content. "
        "No preamble, no commentary, no code fences."
    )
    parts.append(
        "Do not ask clarifying questions. If context is limited, make the "
        "most reasonable implementation-oriented assumptions you can from the "
        "feature name, provided instructions, and repository context."
    )
    parts.append(
        "Return a complete artifact, not notes about what information is missing."
    )
    parts.append(
        "Stay grounded in the requested feature and the existing Turma codebase. "
        "Do not invent unrelated product changes, new user-facing systems, "
        "UI work, microservices, rollout plans, or broad architecture shifts "
        "unless the instructions explicitly require them."
    )
    parts.append(
        "For backend or provider features, focus on concrete code-level changes "
        "such as CLI invocation, backend routing, config, tests, validation, "
        "and smoke-test behavior."
    )
    parts.append(
        "Mirror the existing backend pattern in src/turma/authoring/. "
        "If the repository already contains backends like claude.py or codex.py, "
        "follow that file shape and the existing routing in src/turma/planning.py "
        "instead of inventing new modules, APIs, services, or frameworks."
    )
    parts.append(
        "Do not propose Beads integration, package.json changes, REST/API layers, "
        "or module names outside the current src/turma/authoring/ backend pattern "
        "unless the instructions explicitly require them."
    )
    parts.append(
        "Do not invent new config keys if the existing config format already "
        "supports the feature (e.g. provider/model format in author_model). "
        "Do not add deployment, monitoring, rollout, or production-readiness "
        "tasks. Scope all work to code, tests, config template, and docs "
        "within this repository."
    )

    return "\n\n".join(parts)


def _validate_artifact_output(
    raw: str,
    artifact_id: str,
    template: str,
    feature: str,
) -> str:
    """Reject empty or obviously non-artifact model output."""
    text = raw.strip()
    if not text:
        raise PlanningError(
            f"generating {artifact_id} failed: author returned empty output"
        )

    text = _strip_wrapping_code_fence(text)
    text = _strip_leading_preamble(text)
    lowered = text.lower()
    if any(pattern in lowered for pattern in QUESTION_PATTERNS):
        raise PlanningError(
            f"generating {artifact_id} failed: author asked for clarification "
            "instead of producing the artifact"
        )

    if "?" in text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines and all(re.search(r"\?$", line) for line in lines[:2]):
            raise PlanningError(
                f"generating {artifact_id} failed: author returned a follow-up "
                "question instead of the artifact"
            )

    missing_headings = [
        heading for heading in _extract_template_headings(template)
        if heading not in text
    ]
    if missing_headings:
        raise PlanningError(
            f"generating {artifact_id} failed: output is missing required "
            f"template headings: {', '.join(missing_headings)}"
        )

    _validate_feature_relevance(text, artifact_id, feature)

    return text + "\n"


def _validate_feature_relevance(
    text: str,
    artifact_id: str,
    feature: str,
) -> None:
    """Reject obviously off-target artifacts for backend/provider feature work."""
    tokens = set(re.findall(r"[a-z0-9]+", feature.lower()))
    if not tokens.intersection(BACKEND_FEATURE_TOKENS):
        return

    lowered = text.lower()
    for pattern in OFF_TARGET_BACKEND_PATTERNS:
        if pattern in lowered:
            raise PlanningError(
                f"generating {artifact_id} failed: output drifted into unrelated "
                f"product-planning content ({pattern})"
            )

    if "opencode_planning.py" in lowered:
        raise PlanningError(
            f"generating {artifact_id} failed: output invented a backend module "
            "instead of reusing the existing authoring backend pattern"
        )

    known_backends = {
        "src/turma/authoring/claude.py",
        "src/turma/authoring/codex.py",
        "src/turma/authoring/opencode.py",
        "src/turma/authoring/gemini.py",
    }
    if "src/turma/authoring/" in lowered and not any(
        b in lowered for b in known_backends
    ):
        raise PlanningError(
            f"generating {artifact_id} failed: output referenced an unexpected "
            "backend module path instead of the existing src/turma/authoring/ pattern"
        )


def _strip_leading_preamble(text: str) -> str:
    """Drop conversational lines before the first markdown heading."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^#{1,6}\s+\S", line.strip()):
            return "\n".join(lines[index:]).strip()
    return text


def _strip_wrapping_code_fence(text: str) -> str:
    """Remove a single outer markdown fence if the whole artifact is wrapped."""
    lines = text.splitlines()
    if len(lines) < 3:
        return text

    first = lines[0].strip()
    last = lines[-1].strip()
    if not first.startswith("```") or last != "```":
        return text

    return "\n".join(lines[1:-1]).strip()


def _extract_template_headings(template: str) -> list[str]:
    """Return the markdown headings required by the template."""
    headings = []
    for line in template.splitlines():
        stripped = line.strip()
        if re.match(r"^#{1,6}\s+\S", stripped):
            if "<!--" in stripped or "-->" in stripped:
                continue
            headings.append(stripped)
    return headings
