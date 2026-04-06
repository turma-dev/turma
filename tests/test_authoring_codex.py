"""Tests for the Codex author backend."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from turma.authoring.codex import CodexAuthorBackend
from turma.errors import PlanningError


@patch("turma.authoring.codex.shutil.which", return_value="/usr/bin/codex")
@patch("turma.authoring.codex.subprocess.run")
def test_generate_reads_output_file_on_success(
    mock_run: MagicMock,
    mock_which: MagicMock,
    tmp_path: Path,
) -> None:
    """Codex backend returns the captured final message file content."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = "session transcript"
    result.stderr = ""
    mock_run.return_value = result

    backend = CodexAuthorBackend()

    output_file = tmp_path / "out.md"
    with patch(
        "turma.authoring.codex.tempfile.mkstemp",
        return_value=(123, str(output_file)),
    ), patch("turma.authoring.codex.os.close"), patch(
        "turma.authoring.codex.Path.unlink"
    ) as mock_unlink:
        output_file.write_text("## Why\nText")
        assert backend.generate("prompt", "gpt-5.4", 300) == "## Why\nText"
        mock_unlink.assert_called_once()
        assert mock_run.call_args.args[0] == [
            "codex",
            "exec",
            "prompt",
            "--model",
            "gpt-5.4",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--output-last-message",
            str(output_file),
        ]


@patch("turma.authoring.codex.shutil.which", return_value="/usr/bin/codex")
@patch("turma.authoring.codex.subprocess.run")
def test_generate_raises_on_non_zero_exit(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """Codex backend surfaces subprocess failure details."""
    result = MagicMock()
    result.returncode = 1
    result.stdout = ""
    result.stderr = "codex exploded"
    mock_run.return_value = result

    backend = CodexAuthorBackend()

    with pytest.raises(PlanningError, match="codex exploded"):
        backend.generate("prompt", "gpt-5.4", 300)


@patch("turma.authoring.codex.shutil.which", return_value="/usr/bin/codex")
@patch(
    "turma.authoring.codex.subprocess.run",
    side_effect=subprocess.TimeoutExpired("codex", 300),
)
def test_generate_raises_on_timeout(
    mock_run: MagicMock,
    mock_which: MagicMock,
) -> None:
    """Codex backend raises PlanningError on timeout."""
    backend = CodexAuthorBackend()

    with pytest.raises(PlanningError, match="timed out after 300s"):
        backend.generate("prompt", "gpt-5.4", 300)


@patch("turma.authoring.codex.shutil.which", return_value="/usr/bin/codex")
@patch("turma.authoring.codex.subprocess.run")
def test_generate_fails_when_output_file_missing(
    mock_run: MagicMock,
    mock_which: MagicMock,
    tmp_path: Path,
) -> None:
    """Codex backend fails clearly when no final-output file is written."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = "session transcript"
    result.stderr = ""
    mock_run.return_value = result

    backend = CodexAuthorBackend()

    output_file = tmp_path / "missing.md"
    with patch(
        "turma.authoring.codex.tempfile.mkstemp",
        return_value=(123, str(output_file)),
    ), patch("turma.authoring.codex.os.close"):
        with pytest.raises(PlanningError, match="did not write the final output file"):
            backend.generate("prompt", "gpt-5.4", 300)


@patch("turma.authoring.codex.shutil.which", return_value=None)
def test_backend_init_requires_codex_cli(mock_which: MagicMock) -> None:
    """Codex backend validates CLI presence on initialization."""
    with pytest.raises(PlanningError, match="codex CLI not found"):
        CodexAuthorBackend()
