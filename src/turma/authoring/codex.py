"""Codex-backed author generation."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import os
from pathlib import Path

from turma.authoring.base import AuthorBackend, extract_process_error
from turma.errors import PlanningError


class CodexAuthorBackend(AuthorBackend):
    """Run Codex as the planning author backend."""

    def __init__(self) -> None:
        if shutil.which("codex") is None:
            raise PlanningError(
                "codex CLI not found. Install Codex first."
            )

    def generate(self, prompt: str, model: str, timeout: int) -> str:
        """Return Codex's final message for the prompt."""
        fd, output_path_raw = tempfile.mkstemp(prefix="turma-codex-", suffix=".md")
        output_path = Path(output_path_raw)
        os.close(fd)

        try:
            try:
                result = subprocess.run(
                    [
                        "codex",
                        "exec",
                        prompt,
                        "--model",
                        model,
                        "--sandbox",
                        "read-only",
                        "--skip-git-repo-check",
                        "--output-last-message",
                        str(output_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise PlanningError(
                    f"codex author generation timed out after {timeout}s"
                ) from exc

            if result.returncode != 0:
                raise PlanningError(
                    extract_process_error(result, provider_name="codex")
                )

            if not output_path.exists():
                raise PlanningError(
                    "codex author generation failed: codex did not write the final output file"
                )

            return output_path.read_text()
        finally:
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass
