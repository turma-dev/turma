"""Planning orchestration for the Turma CLI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from turma.authoring.base import AuthorBackend
from turma.authoring.claude import ClaudeAuthorBackend
from turma.config import ConfigError, load_config
from turma.errors import PlanningError

ARTIFACT_ORDER = ["proposal", "design", "tasks"]
QUESTION_PATTERNS = (
    "could you clarify",
    "can you clarify",
    "i need to understand",
    "i don't have context",
    "my best guess",
    "which direction should i take",
)


def run_planning(feature: str) -> None:
    """Run single-pass author planning for a feature."""
    # Step 1: Load config
    try:
        config = load_config()
    except ConfigError as exc:
        raise PlanningError(str(exc)) from exc

    author_model = config.planning.author_model

    # Step 2: Validate .agents/author.md
    author_path = Path.cwd() / ".agents" / "author.md"
    if not author_path.exists():
        raise PlanningError(
            ".agents/author.md not found. Create it before running turma plan."
        )
    author_role = author_path.read_text()

    # Step 3: Check CLIs on PATH
    if shutil.which("openspec") is None:
        raise PlanningError(
            "openspec CLI not found. Install it: npm install -g @fission-ai/openspec"
        )
    backend = _get_backend(author_model)

    # Step 4: Fail if change already exists
    change_dir = Path.cwd() / "openspec" / "changes" / feature
    if change_dir.exists():
        raise PlanningError(
            f"openspec/changes/{feature}/ already exists. "
            "Remove it or pick a different feature name."
        )

    print("loading config from turma.toml")
    print(f"author model: {author_model}")
    print(f"creating change: {feature}")

    # Step 5: Scaffold change
    _run_openspec(
        ["openspec", "new", "change", feature],
        step=f"scaffolding change {feature}",
    )

    # Step 6: Generate artifacts in fixed order
    written_artifacts: dict[str, Path] = {}

    for artifact_id in ARTIFACT_ORDER:
        print(f"generating {artifact_id} (this may take 1-2 min) ...", end=" ", flush=True)

        instructions = _get_instructions(artifact_id, feature)
        output_path = change_dir / instructions["outputPath"]

        # Read dependency content
        dep_content = _read_dependencies(instructions, written_artifacts)

        # Assemble prompt
        prompt = _build_prompt(
            author_role=author_role,
            instructions=instructions,
            dep_content=dep_content,
            feature=feature,
        )

        # Run claude
        raw_output = backend.generate(prompt, author_model, timeout=300)

        # Write output
        artifact_text = _validate_artifact_output(
            raw_output,
            artifact_id,
            instructions.get("template", ""),
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(artifact_text)
        written_artifacts[artifact_id] = output_path

        print("done")

    # Step 7: Summary
    print(f"\nplanning complete. artifacts written to openspec/changes/{feature}/")


def _get_backend(model: str) -> AuthorBackend:
    """Return the author backend for the configured model."""
    if model.startswith("claude-"):
        return ClaudeAuthorBackend()
    raise PlanningError(
        f"unsupported planning author model: {model}. "
        "Only claude-* models are supported in v1."
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


def _get_instructions(artifact_id: str, feature: str) -> dict:
    """Get openspec instructions JSON for an artifact."""
    result = _run_openspec(
        [
            "openspec", "instructions", artifact_id,
            "--change", feature, "--json",
        ],
        step=f"loading instructions for {artifact_id}",
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
        f"<instructions>\n{instruction}\n</instructions>",
        f"<template>\n{template}\n</template>",
    ]

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

    return "\n\n".join(parts)


def _validate_artifact_output(
    raw: str,
    artifact_id: str,
    template: str,
) -> str:
    """Reject empty or obviously non-artifact model output."""
    text = raw.strip()
    if not text:
        raise PlanningError(
            f"generating {artifact_id} failed: claude returned empty output"
        )

    text = _strip_leading_preamble(text)
    lowered = text.lower()
    if any(pattern in lowered for pattern in QUESTION_PATTERNS):
        raise PlanningError(
            f"generating {artifact_id} failed: claude asked for clarification "
            "instead of producing the artifact"
        )

    if "?" in text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines and all(re.search(r"\?$", line) for line in lines[:2]):
            raise PlanningError(
                f"generating {artifact_id} failed: claude returned a follow-up "
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

    return text + "\n"


def _strip_leading_preamble(text: str) -> str:
    """Drop conversational lines before the first markdown heading."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^#{1,6}\s+\S", line.strip()):
            return "\n".join(lines[index:]).strip()
    return text


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
