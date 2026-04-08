"""Tests for the Gemini author backend."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from turma.authoring.gemini import GeminiAuthorBackend
from turma.errors import PlanningError


@patch("turma.authoring.gemini.shutil.which", return_value="/usr/bin/gemini")
@patch("turma.authoring.gemini.subprocess.run")
def test_generate_returns_stdout_on_success(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """Gemini backend returns raw stdout on success."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = "## Why\nText"
    result.stderr = ""
    mock_run.return_value = result

    backend = GeminiAuthorBackend()

    assert backend.generate("prompt", "gemini-2.5-flash", 300) == "## Why\nText"


@patch("turma.authoring.gemini.shutil.which", return_value="/usr/bin/gemini")
@patch("turma.authoring.gemini.subprocess.run")
def test_generate_raises_on_non_zero_exit(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """Gemini backend surfaces stderr on failure."""
    result = MagicMock()
    result.returncode = 1
    result.stdout = ""
    result.stderr = "gemini exploded"
    mock_run.return_value = result

    backend = GeminiAuthorBackend()

    with pytest.raises(PlanningError, match="gemini exploded"):
        backend.generate("prompt", "gemini-2.5-flash", 300)


@patch("turma.authoring.gemini.shutil.which", return_value="/usr/bin/gemini")
@patch("turma.authoring.gemini.subprocess.run")
def test_generate_uses_stdout_when_stderr_empty(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """Gemini backend falls back to stdout when stderr is empty."""
    result = MagicMock()
    result.returncode = 1
    result.stdout = "GEMINI_API_KEY not set"
    result.stderr = ""
    mock_run.return_value = result

    backend = GeminiAuthorBackend()

    with pytest.raises(PlanningError, match="GEMINI_API_KEY not set"):
        backend.generate("prompt", "gemini-2.5-flash", 300)


@patch("turma.authoring.gemini.shutil.which", return_value="/usr/bin/gemini")
@patch(
    "turma.authoring.gemini.subprocess.run",
    side_effect=subprocess.TimeoutExpired("gemini", 300),
)
def test_generate_raises_on_timeout(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """Gemini backend raises PlanningError on timeout."""
    backend = GeminiAuthorBackend()

    with pytest.raises(PlanningError, match="timed out after 300s"):
        backend.generate("prompt", "gemini-2.5-flash", 300)


@patch("turma.authoring.gemini.shutil.which", return_value=None)
def test_backend_init_requires_gemini_cli(mock_which: MagicMock) -> None:
    """Gemini backend validates CLI presence on initialization."""
    with pytest.raises(PlanningError, match="gemini CLI not found"):
        GeminiAuthorBackend()


@patch("turma.authoring.gemini.shutil.which", return_value="/usr/bin/gemini")
@patch("turma.authoring.gemini.subprocess.run")
def test_generate_uses_correct_command_structure(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """Gemini backend invokes gemini with the correct arguments."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = "## Why\nText"
    result.stderr = ""
    mock_run.return_value = result

    backend = GeminiAuthorBackend()
    backend.generate("the prompt", "gemini-2.5-flash", 300)

    assert mock_run.call_args.args[0] == [
        "gemini",
        "-p",
        "the prompt",
        "-m",
        "gemini-2.5-flash",
        "--approval-mode",
        "plan",
    ]
