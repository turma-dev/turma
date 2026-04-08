"""Gemini-backed author generation."""

from __future__ import annotations

import shutil
import subprocess

from turma.authoring.base import AuthorBackend, extract_process_error
from turma.errors import PlanningError


class GeminiAuthorBackend(AuthorBackend):
    """Run Gemini CLI as the planning author backend."""

    def __init__(self) -> None:
        if shutil.which("gemini") is None:
            raise PlanningError(
                "gemini CLI not found. Install it: npm install -g @google/gemini-cli"
            )

    def generate(self, prompt: str, model: str, timeout: int) -> str:
        """Return Gemini's raw text output for the prompt.

        Safety note: ``--approval-mode plan`` enforces read-only tool
        policy.  Gemini CLI silently downgrades this to "default" in
        untrusted folders, but ``-p`` (headless mode) causes the CLI to
        treat the folder as trusted, so the downgrade does not fire for
        our invocation.  If Gemini changes its headless-trust behaviour,
        this safety boundary could break silently.
        """
        try:
            result = subprocess.run(
                [
                    "gemini",
                    "-p",
                    prompt,
                    "-m",
                    model,
                    "--approval-mode",
                    "plan",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PlanningError(
                f"gemini author generation timed out after {timeout}s"
            ) from exc

        if result.returncode != 0:
            raise PlanningError(
                extract_process_error(result, provider_name="gemini")
            )

        return result.stdout
