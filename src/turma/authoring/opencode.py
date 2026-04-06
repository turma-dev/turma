"""OpenCode-backed author generation."""

from __future__ import annotations

import shutil
import subprocess

from turma.authoring.base import AuthorBackend, extract_process_error
from turma.errors import PlanningError


class OpenCodeAuthorBackend(AuthorBackend):
    """Run OpenCode as the planning author backend."""

    def __init__(self) -> None:
        if shutil.which("opencode") is None:
            raise PlanningError(
                "opencode CLI not found. Install OpenCode first."
            )

    def generate(self, prompt: str, model: str, timeout: int) -> str:
        """Return OpenCode's raw text output for the prompt."""
        try:
            result = subprocess.run(
                [
                    "opencode",
                    "run",
                    "--model",
                    model,
                    prompt,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PlanningError(
                f"opencode author generation timed out after {timeout}s"
            ) from exc

        if result.returncode != 0:
            raise PlanningError(
                extract_process_error(result, provider_name="opencode")
            )

        return result.stdout
