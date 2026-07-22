"""Run-event emitter seam for `turma run`.

The orchestrator narrates its progress through a `RunEmitter` rather than
raw `print(...)`. Two implementations:

- `TextEmitter` renders each event as the exact operator-facing line(s) the
  orchestrator has always printed — byte-for-byte, guarded by the existing
  text assertions in `tests/test_swarm_run.py`.
- `JsonEmitter` renders each event as one compact NDJSON object
  (`{"schema": "turma.run.v1", "event": ..., ...}`) per line and flushes,
  for `turma run --json` (live surfaces: VS Code / MCP / dashboards).

See `openspec/changes/run-json-events/` for the event catalog and contract.
Emit sites pass structured fields; `TextEmitter` owns the format strings so
the two emitters stay in sync from one call.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Protocol, TextIO

RUN_SCHEMA = "turma.run.v1"

# Envelope fields the emitter owns; a caller must not pass them as event
# fields (that would break the one-run_id / fresh-ts / fixed-schema contract).
_RESERVED_EVENT_FIELDS = frozenset({"schema", "event", "run_id", "ts"})


class RunEmitter(Protocol):
    """Runtime shape for a run-event sink."""

    def emit(self, event: str, /, **fields: object) -> None: ...


# ---------------------------------------------------------------------
# Text rendering — byte-for-byte with the historical print() lines
# ---------------------------------------------------------------------


def _render_text(event: str, fields: dict[str, object]) -> str:
    """Return the exact operator-facing line for an event.

    Each branch reproduces the f-string that used to live inline at the
    corresponding `print(...)` site. The existing text tests pin these.
    """
    f = fields
    if event == "fetch_skipped":
        return "fetch: skipped (--dry-run)"
    if event == "fetch_advanced":
        b = f["base_branch"]
        return f"fetch: origin/{b} → {b}"
    if event == "reconcile_summary":
        n = f["in_progress_count"]
        noun = "task" if n == 1 else "tasks"
        return f"reconcile: {n} in-progress {noun}"
    if event == "reconcile_skipped":
        return (
            f"reconcile:   {f['task_id']} → skipped "
            f"(merge-tracked at PR #{f['pr_number']})"
        )
    if event == "reconcile_finding":
        return f"reconcile:   {f['detail']}"
    if event == "repair":
        action = f["action"]
        task_id = f["task_id"]
        if action == "release_claim_missing_worktree":
            return f"repair: {task_id} → release claim (missing-worktree)"
        if action == "completion_pending":
            return (
                f"repair: {task_id} → committed, pushed, PR opened "
                f"({f['pr_url']}; awaiting merge), labelled"
            )
        if action == "completion_pending_with_pr":
            return (
                f"repair: {task_id} → labelled "
                f"(PR already open at {f['pr_url']}; awaiting merge)"
            )
        if action == "fail_task":
            return f"repair: {task_id} → fail_task recorded ({f['reason']})"
        raise KeyError(f"unknown repair action: {action!r}")
    if event == "repair_orphan_branch":
        return f"repair: orphan branch (operator triage): {f['branch']}"
    if event == "merge_advancement":
        task_id = f["task_id"]
        action = f["action"]
        prefix = "would: " if f.get("dry_run") else ""
        if action == "closed":
            return f"{prefix}merge-advancement: {task_id} → MERGED, closed"
        if action == "failed":
            return (
                f"{prefix}merge-advancement: {task_id} → CLOSED "
                "without merge → fail_task"
            )
        if action == "left_alone":
            return f"merge-advancement: {task_id} → OPEN, leaving alone"
        if action == "left_alone_unrecognized":
            return (
                f"merge-advancement: {task_id} → "
                f"unrecognized state {f['pr_state']!r}, leaving alone"
            )
        if action == "halting_stale":
            return (
                f"merge-advancement: {task_id} → 404; halting "
                f"(turma-pr:{f['pr_number']} stale; triage)"
            )
        raise KeyError(f"unknown merge_advancement action: {action!r}")
    if event == "stopping_max_tasks":
        return f"swarm: stopping at --max-tasks={f['max_tasks']}"
    if event == "done":
        return "swarm: no ready tasks remain; done"
    if event == "claim_race":
        return f"swarm: claim race on {f['task_id']}; skipping ({f['detail']})"
    if event == "task_claimed":
        return f"swarm: claimed {f['task_id']} — {f['title']}"
    if event == "worktree_setup":
        return f"worktree: setup {f['task_id']}"
    if event == "worker_running":
        return f"worker: running {f['task_id']} (timeout {f['timeout_s']}s)"
    if event == "commit":
        return f"commit: {f['task_id']}"
    if event == "push":
        return f"push: {f['task_id']}"
    if event == "task_opened":
        return (
            f"swarm: opened {f['task_id']} "
            f"(PR: {f['pr_url']}; awaiting merge)"
        )
    if event == "task_failed":
        task_id = f["task_id"]
        if f["budget_exhausted"]:
            return (
                f"swarm: {task_id} failed (budget exhausted after "
                f"{f['attempt']} attempts): {f['reason']}"
            )
        return (
            f"swarm: {task_id} failed (attempt "
            f"{f['attempt']}/{f['max_attempts']}): {f['reason']}"
        )
    raise KeyError(f"unknown run event: {event!r}")


class TextEmitter:
    """Emit run events as the historical operator-facing text lines.

    Writes are serialized by a lock: the concurrent dispatcher emits task events
    from several worker threads (a pooled `turma run` without `--json` too), so
    the `print()` calls must not interleave. Lifecycle / heartbeat events are
    `--json`-gated and never reach this emitter — `_render_text` is unchanged and
    still raises on a genuinely unknown event.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()

    def emit(self, event: str, /, **fields: object) -> None:
        line = _render_text(event, fields)  # render (and validate) outside the lock
        with self._lock:
            print(line, file=self._stream)


# ---------------------------------------------------------------------
# JSON rendering — one NDJSON object per event, flushed
# ---------------------------------------------------------------------


class JsonEmitter:
    """Emit run events as `turma.run.v1` NDJSON, one object per line.

    Every event carries a per-run `run_id` (a `uuid4` hex, one per invocation, so
    interleaved concurrent events are correlatable) and a `ts` (ISO-8601 UTC,
    stamped at emit time, so consumers order by real time not stream position).
    Flushes after each event so streaming consumers (UI / MCP) see progress live.

    Writes are serialized by a lock so concurrent worker-thread events and the
    heartbeat stay line-atomic. The lock is held only around `write` + `flush`
    (the JSON is built outside it); emit is line-atomic and synchronous — it does
    not decouple the run from a slow consumer stream.

    `text`-only fields (used solely by `TextEmitter`) are dropped from the payload
    via the emit-site contract: callers pass structured fields; nothing
    text-shaped is passed that shouldn't be in the JSON.
    """

    def __init__(
        self, stream: TextIO | None = None, run_id: str | None = None
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._run_id = run_id if run_id is not None else uuid.uuid4().hex
        self._lock = threading.Lock()

    @property
    def run_id(self) -> str:
        return self._run_id

    def emit(self, event: str, /, **fields: object) -> None:
        conflicts = _RESERVED_EVENT_FIELDS.intersection(fields)
        if conflicts:
            raise ValueError(
                f"run event {event!r} passed reserved envelope field(s) "
                f"{sorted(conflicts)}; schema/event/run_id/ts are owned by the "
                "emitter and must not be set by callers"
            )
        payload = {
            "schema": RUN_SCHEMA,
            "event": event,
            "run_id": self._run_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        line = json.dumps(payload) + "\n"
        with self._lock:
            self._stream.write(line)
            self._stream.flush()


class HeartbeatTicker:
    """Emit periodic `heartbeat` events on a daemon thread until stopped.

    The CLI starts this around the execution phase of a `--json` run so a
    consumer can tell a live run from one hung inside a long blocking
    `worker.run` (up to `worker_timeout`) — no natural in-loop emit fires during
    that stretch, so only a timer keeps the stream alive.

    `interval_seconds <= 0` disables it (no thread spawned). `elapsed_ms` on each
    event is measured from `started_at` (a `time.monotonic()` value the caller
    captured at run start). Stop is immediate: the wait *is* the interval, and
    `stop()` sets the event the wait watches.
    """

    def __init__(
        self,
        emitter: RunEmitter,
        interval_seconds: float,
        started_at: float,
    ) -> None:
        self._emitter = emitter
        self._interval = interval_seconds
        self._started_at = started_at
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "HeartbeatTicker":
        if self._interval > 0:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._emitter.emit(
                "heartbeat",
                elapsed_ms=int((time.monotonic() - self._started_at) * 1000),
            )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 1.0)
