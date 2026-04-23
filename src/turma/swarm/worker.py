"""Worker-backend protocol and the v1 Claude Code implementation.

The swarm orchestrator drives a claimed Beads task by handing a
`WorkerInvocation` to a `WorkerBackend`, which runs the agent CLI
non-interactively inside the per-task worktree and returns a typed
`WorkerResult`. Completion is signaled via filesystem sentinels the
worker is prompted to write (`.task_complete` on success,
`.task_failed` on an unresolvable blocker); the orchestrator does not
parse the worker's stdout for success/failure.

v1 ships a single backend — `ClaudeCodeWorker` — registered under the
name `"claude-code"`. Adding Codex, OpenCode, or Gemini later is a
small follow-on: implement the protocol, register the factory.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from turma.errors import PlanningError


# ---------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------


WorkerStatus = Literal["success", "failure", "timeout"]


@dataclass(frozen=True)
class WorkerInvocation:
    """Everything the worker needs to drive a single task."""

    task_id: str
    title: str
    description: str
    worktree: Path
    timeout_seconds: int


@dataclass(frozen=True)
class WorkerResult:
    """Outcome of a worker run.

    `reason` is empty when `status == "success"`. For failure, it is
    either the contents of `.task_failed` (if the worker wrote one)
    or a canned message describing the missing-sentinel case. For
    timeout, it is the fixed string `"worker exceeded timeout"`.
    """

    status: WorkerStatus
    reason: str
    stdout: str
    stderr: str


class WorkerBackend(Protocol):
    """Runtime shape every worker adapter honors."""

    name: str

    def run(self, invocation: WorkerInvocation) -> WorkerResult:
        ...


# ---------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------


WORKER_PROMPT_TEMPLATE = """\
You are a Turma worker agent. Work inside {worktree}.

Task: {title}

Acceptance criteria:
{description}

When you believe the task is complete, write `DONE` to
`.task_complete` in this directory. If you hit a blocker you can
not resolve, write the reason to `.task_failed` and stop.\
"""


def render_worker_prompt(invocation: WorkerInvocation) -> str:
    """Render the pinned worker prompt template for an invocation."""
    return WORKER_PROMPT_TEMPLATE.format(
        worktree=invocation.worktree,
        title=invocation.title,
        description=invocation.description,
    )


# ---------------------------------------------------------------------
# Sentinel detection
# ---------------------------------------------------------------------


TASK_COMPLETE_SENTINEL = ".task_complete"
TASK_FAILED_SENTINEL = ".task_failed"
_MISSING_MARKER_REASON = (
    "worker exited without writing a completion marker"
)
_TIMEOUT_REASON = "worker exceeded timeout"


def _detect_sentinel_result(
    worktree: Path, stdout: str, stderr: str
) -> WorkerResult:
    """Derive a WorkerResult from the sentinels the worker (may have) written.

    `.task_complete` takes precedence over `.task_failed` so a worker
    that writes both (e.g. wrote failed then changed its mind) is
    interpreted as having completed the task.
    """
    complete_path = worktree / TASK_COMPLETE_SENTINEL
    failed_path = worktree / TASK_FAILED_SENTINEL

    if complete_path.exists():
        return WorkerResult(
            status="success", reason="", stdout=stdout, stderr=stderr
        )
    if failed_path.exists():
        try:
            reason = failed_path.read_text().strip() or "unspecified"
        except OSError as exc:
            reason = f"could not read {TASK_FAILED_SENTINEL}: {exc}"
        return WorkerResult(
            status="failure", reason=reason, stdout=stdout, stderr=stderr
        )
    return WorkerResult(
        status="failure",
        reason=_MISSING_MARKER_REASON,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------
# Claude Code worker
# ---------------------------------------------------------------------


CLAUDE_INSTALL_HINT = (
    "claude CLI not found. Install Claude Code first "
    "(https://claude.ai/code)."
)


class ClaudeCodeWorker:
    """Runs Claude Code non-interactively inside a per-task worktree.

    argv: `claude -p <prompt> --dangerously-skip-permissions`
    cwd:  the worktree (set via `subprocess.run(cwd=...)`); Claude Code
          does not expose a `--cwd` flag, so we use subprocess's cwd
          parameter.
    """

    name = "claude-code"

    def __init__(self) -> None:
        if shutil.which("claude") is None:
            raise PlanningError(CLAUDE_INSTALL_HINT)

    def run(self, invocation: WorkerInvocation) -> WorkerResult:
        prompt = render_worker_prompt(invocation)
        argv = [
            "claude",
            "-p", prompt,
            "--dangerously-skip-permissions",
        ]
        try:
            result = subprocess.run(
                argv,
                cwd=invocation.worktree,
                capture_output=True,
                text=True,
                timeout=invocation.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "").decode() if isinstance(
                exc.stdout, (bytes, bytearray)
            ) else (exc.stdout or "")
            stderr = (exc.stderr or "").decode() if isinstance(
                exc.stderr, (bytes, bytearray)
            ) else (exc.stderr or "")
            return WorkerResult(
                status="timeout",
                reason=_TIMEOUT_REASON,
                stdout=stdout,
                stderr=stderr,
            )

        return _detect_sentinel_result(
            worktree=invocation.worktree,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )


# ---------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------


_BACKENDS: dict[str, Callable[[], WorkerBackend]] = {
    "claude-code": ClaudeCodeWorker,
}


def get_worker_backend(name: str) -> WorkerBackend:
    """Return a fresh instance of the named worker backend.

    Raises `PlanningError` if the name is not registered so the CLI
    can surface the mistake before any Beads state is mutated.
    """
    factory = _BACKENDS.get(name)
    if factory is None:
        raise PlanningError(
            f"unknown worker backend: {name!r}. "
            f"Registered: {sorted(_BACKENDS)}"
        )
    return factory()


def registered_worker_backends() -> tuple[str, ...]:
    """Return the sorted tuple of registered backend names."""
    return tuple(sorted(_BACKENDS))
