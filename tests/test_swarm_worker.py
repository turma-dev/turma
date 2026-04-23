"""Tests for the WorkerBackend protocol and ClaudeCodeWorker."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from turma.errors import PlanningError
from turma.swarm.worker import (
    CLAUDE_INSTALL_HINT,
    TASK_COMPLETE_SENTINEL,
    TASK_FAILED_SENTINEL,
    WORKER_PROMPT_TEMPLATE,
    ClaudeCodeWorker,
    WorkerBackend,
    WorkerInvocation,
    WorkerResult,
    _detect_sentinel_result,
    get_worker_backend,
    registered_worker_backends,
    render_worker_prompt,
)


def _inv(
    tmp_path: Path,
    *,
    task_id: str = "bd-1",
    title: str = "Do the thing",
    description: str = "- [ ] step one\n- [ ] step two",
    timeout_seconds: int = 30,
) -> WorkerInvocation:
    return WorkerInvocation(
        task_id=task_id,
        title=title,
        description=description,
        worktree=tmp_path,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------


def test_prompt_template_contains_expected_markers() -> None:
    # Guard the contract on the template itself so a stray edit
    # surfaces loudly.
    assert "{worktree}" in WORKER_PROMPT_TEMPLATE
    assert "{title}" in WORKER_PROMPT_TEMPLATE
    assert "{description}" in WORKER_PROMPT_TEMPLATE
    assert "`.task_complete`" in WORKER_PROMPT_TEMPLATE
    assert "`.task_failed`" in WORKER_PROMPT_TEMPLATE


def test_render_worker_prompt_substitutes_invocation_fields(
    tmp_path: Path,
) -> None:
    invocation = _inv(
        tmp_path,
        title="Extract primitives",
        description="- [ ] Split module\n- [ ] Add injection seam",
    )
    prompt = render_worker_prompt(invocation)

    assert str(tmp_path) in prompt
    assert "Task: Extract primitives" in prompt
    assert "- [ ] Split module" in prompt
    assert "- [ ] Add injection seam" in prompt


# ---------------------------------------------------------------------
# Sentinel detection
# ---------------------------------------------------------------------


def test_sentinel_complete_takes_precedence_over_failed(
    tmp_path: Path,
) -> None:
    (tmp_path / TASK_COMPLETE_SENTINEL).write_text("DONE\n")
    (tmp_path / TASK_FAILED_SENTINEL).write_text("stale failure\n")

    result = _detect_sentinel_result(tmp_path, stdout="out", stderr="err")

    assert result.status == "success"
    assert result.reason == ""
    assert result.stdout == "out"
    assert result.stderr == "err"


def test_sentinel_failed_surfaces_file_contents_as_reason(
    tmp_path: Path,
) -> None:
    (tmp_path / TASK_FAILED_SENTINEL).write_text(
        "gave up: dependency unavailable\n"
    )

    result = _detect_sentinel_result(tmp_path, stdout="out", stderr="err")

    assert result.status == "failure"
    assert result.reason == "gave up: dependency unavailable"


def test_sentinel_failed_with_empty_file_uses_unspecified_reason(
    tmp_path: Path,
) -> None:
    (tmp_path / TASK_FAILED_SENTINEL).write_text("")

    result = _detect_sentinel_result(tmp_path, stdout="", stderr="")

    assert result.status == "failure"
    assert result.reason == "unspecified"


def test_sentinel_neither_present_fails_with_missing_marker_message(
    tmp_path: Path,
) -> None:
    result = _detect_sentinel_result(tmp_path, stdout="out", stderr="err")

    assert result.status == "failure"
    assert "without writing a completion marker" in result.reason
    assert result.stdout == "out"


# ---------------------------------------------------------------------
# ClaudeCodeWorker.__init__
# ---------------------------------------------------------------------


@patch("turma.swarm.worker.shutil.which", return_value="/usr/bin/claude")
def test_claude_worker_init_succeeds_when_cli_present(
    _which: MagicMock,
) -> None:
    worker = ClaudeCodeWorker()
    assert worker.name == "claude-code"


@patch("turma.swarm.worker.shutil.which", return_value=None)
def test_claude_worker_init_raises_when_cli_missing(
    _which: MagicMock,
) -> None:
    with pytest.raises(PlanningError) as exc:
        ClaudeCodeWorker()
    assert str(exc.value) == CLAUDE_INSTALL_HINT


def test_install_hint_wording() -> None:
    assert "claude CLI not found" in CLAUDE_INSTALL_HINT
    assert "claude.ai/code" in CLAUDE_INSTALL_HINT


# ---------------------------------------------------------------------
# ClaudeCodeWorker.run — argv and cwd pinning
# ---------------------------------------------------------------------


@patch("turma.swarm.worker.shutil.which", return_value="/usr/bin/claude")
@patch("turma.swarm.worker.subprocess.run")
def test_claude_worker_run_pins_argv_and_cwd(
    mock_run: MagicMock, _which: MagicMock, tmp_path: Path
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout="claude output", stderr=""
    )
    (tmp_path / TASK_COMPLETE_SENTINEL).write_text("DONE\n")

    worker = ClaudeCodeWorker()
    result = worker.run(_inv(tmp_path))

    mock_run.assert_called_once()
    call_args = mock_run.call_args
    argv = call_args.args[0]
    assert argv[:2] == ["claude", "-p"]
    assert argv[-1] == "--dangerously-skip-permissions"
    # Prompt is the third argument.
    assert "Task: Do the thing" in argv[2]

    assert call_args.kwargs["cwd"] == tmp_path
    assert call_args.kwargs["capture_output"] is True
    assert call_args.kwargs["text"] is True
    assert call_args.kwargs["timeout"] == 30

    assert result.status == "success"


# ---------------------------------------------------------------------
# ClaudeCodeWorker.run — sentinel dispatch
# ---------------------------------------------------------------------


@patch("turma.swarm.worker.shutil.which", return_value="/usr/bin/claude")
@patch("turma.swarm.worker.subprocess.run")
def test_claude_worker_run_success_sentinel(
    mock_run: MagicMock, _which: MagicMock, tmp_path: Path
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout="output", stderr="warn"
    )
    (tmp_path / TASK_COMPLETE_SENTINEL).write_text("DONE\n")

    result = ClaudeCodeWorker().run(_inv(tmp_path))

    assert result == WorkerResult(
        status="success", reason="", stdout="output", stderr="warn"
    )


@patch("turma.swarm.worker.shutil.which", return_value="/usr/bin/claude")
@patch("turma.swarm.worker.subprocess.run")
def test_claude_worker_run_failure_sentinel(
    mock_run: MagicMock, _which: MagicMock, tmp_path: Path
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout="", stderr=""
    )
    (tmp_path / TASK_FAILED_SENTINEL).write_text("blocked by missing dep\n")

    result = ClaudeCodeWorker().run(_inv(tmp_path))

    assert result.status == "failure"
    assert result.reason == "blocked by missing dep"


@patch("turma.swarm.worker.shutil.which", return_value="/usr/bin/claude")
@patch("turma.swarm.worker.subprocess.run")
def test_claude_worker_run_no_sentinel_is_failure(
    mock_run: MagicMock, _which: MagicMock, tmp_path: Path
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout="", stderr=""
    )
    # No sentinels written.

    result = ClaudeCodeWorker().run(_inv(tmp_path))

    assert result.status == "failure"
    assert "without writing a completion marker" in result.reason


# ---------------------------------------------------------------------
# ClaudeCodeWorker.run — timeout
# ---------------------------------------------------------------------


@patch("turma.swarm.worker.shutil.which", return_value="/usr/bin/claude")
@patch("turma.swarm.worker.subprocess.run")
def test_claude_worker_run_timeout_returns_typed_timeout(
    mock_run: MagicMock, _which: MagicMock, tmp_path: Path
) -> None:
    mock_run.side_effect = subprocess.TimeoutExpired(
        cmd=["claude"], timeout=30, output=b"partial output", stderr=b"partial err"
    )

    result = ClaudeCodeWorker().run(_inv(tmp_path, timeout_seconds=30))

    assert result.status == "timeout"
    assert result.reason == "worker exceeded timeout"
    assert result.stdout == "partial output"
    assert result.stderr == "partial err"


@patch("turma.swarm.worker.shutil.which", return_value="/usr/bin/claude")
@patch("turma.swarm.worker.subprocess.run")
def test_claude_worker_run_timeout_without_captured_streams(
    mock_run: MagicMock, _which: MagicMock, tmp_path: Path
) -> None:
    """TimeoutExpired sometimes carries None for stdout/stderr."""
    mock_run.side_effect = subprocess.TimeoutExpired(
        cmd=["claude"], timeout=1, output=None, stderr=None
    )

    result = ClaudeCodeWorker().run(_inv(tmp_path, timeout_seconds=1))

    assert result.status == "timeout"
    assert result.stdout == ""
    assert result.stderr == ""


# ---------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------


def test_registered_worker_backends_exposes_claude_code() -> None:
    assert registered_worker_backends() == ("claude-code",)


@patch("turma.swarm.worker.shutil.which", return_value="/usr/bin/claude")
def test_get_worker_backend_returns_claude_code_instance(
    _which: MagicMock,
) -> None:
    worker = get_worker_backend("claude-code")
    assert isinstance(worker, ClaudeCodeWorker)
    assert worker.name == "claude-code"


def test_get_worker_backend_rejects_unknown_name() -> None:
    with pytest.raises(PlanningError) as exc:
        get_worker_backend("vim-swordsman")
    assert "unknown worker backend" in str(exc.value)
    assert "vim-swordsman" in str(exc.value)
    assert "claude-code" in str(exc.value)  # registered names listed


def test_claude_code_worker_satisfies_worker_backend_protocol() -> None:
    # `Protocol` runtime-check needs @runtime_checkable for isinstance;
    # we don't gate on that. This test instead asserts the attributes
    # and signature the orchestrator relies on are present.
    assert hasattr(ClaudeCodeWorker, "name")
    assert callable(getattr(ClaudeCodeWorker, "run", None))
    # WorkerBackend is only a structural protocol used for type hints.
    # Instantiation here is gated on claude being available, so skip.
    _ = WorkerBackend  # keep the import live for readers
