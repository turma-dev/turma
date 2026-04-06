"""Claude-backed author generation."""

from __future__ import annotations

import shutil
import subprocess

from turma.authoring.base import AuthorBackend, extract_process_error
from turma.errors import PlanningError


class ClaudeAuthorBackend(AuthorBackend):
    """Run Claude Code as the planning author backend."""

    def __init__(self) -> None:
        if shutil.which("claude") is None:
            raise PlanningError(
                "claude CLI not found. Install Claude Code first."
            )

    def generate(self, prompt: str, model: str, timeout: int) -> str:
        """Return Claude's raw text output for the prompt."""
        try:
            result = subprocess.run(
                [
                    "claude",
                    "-p",
                    prompt,
                    "--model",
                    model,
                    "--permission-mode",
                    "plan",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PlanningError(
                f"claude author generation timed out after {timeout}s"
            ) from exc

        if result.returncode != 0:
            raise PlanningError(
                extract_process_error(result, provider_name="claude")
            )

        return result.stdout
