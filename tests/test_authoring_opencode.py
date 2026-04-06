"""Tests for the OpenCode author backend."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from turma.authoring.opencode import OpenCodeAuthorBackend
from turma.errors import PlanningError


@patch("turma.authoring.opencode.shutil.which", return_value="/usr/bin/opencode")
@patch("turma.authoring.opencode.subprocess.run")
def test_generate_returns_stdout_on_success(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """OpenCode backend returns raw stdout on success."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = "## Why\nText"
    result.stderr = ""
    mock_run.return_value = result

    backend = OpenCodeAuthorBackend()

    assert backend.generate("prompt", "groq/llama-3.3-70b-versatile", 300) == "## Why\nText"


@patch("turma.authoring.opencode.shutil.which", return_value="/usr/bin/opencode")
@patch("turma.authoring.opencode.subprocess.run")
def test_generate_raises_on_non_zero_exit(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """OpenCode backend surfaces stderr on failure."""
    result = MagicMock()
    result.returncode = 1
    result.stdout = ""
    result.stderr = "opencode exploded"
    mock_run.return_value = result

    backend = OpenCodeAuthorBackend()

    with pytest.raises(PlanningError, match="opencode exploded"):
        backend.generate("prompt", "groq/llama-3.3-70b-versatile", 300)


@patch("turma.authoring.opencode.shutil.which", return_value="/usr/bin/opencode")
@patch("turma.authoring.opencode.subprocess.run")
def test_generate_uses_stdout_when_stderr_empty(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """OpenCode backend falls back to stdout when stderr is empty."""
    result = MagicMock()
    result.returncode = 1
    result.stdout = "GROQ_API_KEY not set"
    result.stderr = ""
    mock_run.return_value = result

    backend = OpenCodeAuthorBackend()

    with pytest.raises(PlanningError, match="GROQ_API_KEY not set"):
        backend.generate("prompt", "groq/llama-3.3-70b-versatile", 300)


@patch("turma.authoring.opencode.shutil.which", return_value="/usr/bin/opencode")
@patch(
    "turma.authoring.opencode.subprocess.run",
    side_effect=subprocess.TimeoutExpired("opencode", 300),
)
def test_generate_raises_on_timeout(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """OpenCode backend raises PlanningError on timeout."""
    backend = OpenCodeAuthorBackend()

    with pytest.raises(PlanningError, match="timed out after 300s"):
        backend.generate("prompt", "groq/llama-3.3-70b-versatile", 300)


@patch("turma.authoring.opencode.shutil.which", return_value=None)
def test_backend_init_requires_opencode_cli(mock_which: MagicMock) -> None:
    """OpenCode backend validates CLI presence on initialization."""
    with pytest.raises(PlanningError, match="opencode CLI not found"):
        OpenCodeAuthorBackend()


@patch("turma.authoring.opencode.shutil.which", return_value="/usr/bin/opencode")
@patch("turma.authoring.opencode.subprocess.run")
def test_generate_uses_correct_command_structure(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """OpenCode backend invokes opencode run with the correct arguments."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = "## Why\nText"
    result.stderr = ""
    mock_run.return_value = result

    backend = OpenCodeAuthorBackend()
    backend.generate("the prompt", "groq/llama-3.3-70b-versatile", 300)

    assert mock_run.call_args.args[0] == [
        "opencode",
        "run",
        "--model",
        "groq/llama-3.3-70b-versatile",
        "the prompt",
    ]
