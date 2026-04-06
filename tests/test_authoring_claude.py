"""Tests for the Claude author backend."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from turma.authoring.claude import ClaudeAuthorBackend
from turma.errors import PlanningError


@patch("turma.authoring.claude.shutil.which", return_value="/usr/bin/claude")
@patch("turma.authoring.claude.subprocess.run")
def test_generate_returns_stdout_on_success(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """Claude backend returns raw stdout on success."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = "## Why\nText"
    result.stderr = ""
    mock_run.return_value = result

    backend = ClaudeAuthorBackend()

    assert backend.generate("prompt", "claude-opus-4-6", 300) == "## Why\nText"


@patch("turma.authoring.claude.shutil.which", return_value="/usr/bin/claude")
@patch("turma.authoring.claude.subprocess.run")
def test_generate_uses_stdout_when_stderr_empty(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """Claude backend falls back to stdout details when stderr is empty."""
    result = MagicMock()
    result.returncode = 1
    result.stdout = "Not logged in · Please run /login"
    result.stderr = ""
    mock_run.return_value = result

    backend = ClaudeAuthorBackend()

    with pytest.raises(PlanningError, match="Not logged in"):
        backend.generate("prompt", "claude-opus-4-6", 300)


@patch("turma.authoring.claude.shutil.which", return_value="/usr/bin/claude")
@patch("turma.authoring.claude.subprocess.run")
def test_generate_raises_on_non_zero_exit(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """Claude backend surfaces stderr on failure."""
    result = MagicMock()
    result.returncode = 1
    result.stdout = ""
    result.stderr = "claude exploded"
    mock_run.return_value = result

    backend = ClaudeAuthorBackend()

    with pytest.raises(PlanningError, match="claude exploded"):
        backend.generate("prompt", "claude-opus-4-6", 300)


@patch("turma.authoring.claude.shutil.which", return_value="/usr/bin/claude")
@patch("turma.authoring.claude.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 300))
def test_generate_raises_on_timeout(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """Claude backend raises PlanningError on timeout."""
    backend = ClaudeAuthorBackend()

    with pytest.raises(PlanningError, match="timed out after 300s"):
        backend.generate("prompt", "claude-opus-4-6", 300)


@patch("turma.authoring.claude.shutil.which", return_value=None)
def test_backend_init_requires_claude_cli(mock_which: MagicMock) -> None:
    """Claude backend validates CLI presence on initialization."""
    with pytest.raises(PlanningError, match="claude CLI not found"):
        ClaudeAuthorBackend()
