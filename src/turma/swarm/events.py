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
from typing import Protocol, TextIO

RUN_SCHEMA = "turma.run.v1"


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
    """Emit run events as the historical operator-facing text lines."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def emit(self, event: str, /, **fields: object) -> None:
        print(_render_text(event, fields), file=self._stream)


# ---------------------------------------------------------------------
# JSON rendering — one NDJSON object per event, flushed
# ---------------------------------------------------------------------


class JsonEmitter:
    """Emit run events as `turma.run.v1` NDJSON, one object per line.

    Flushes after each event so streaming consumers (UI / MCP) see
    progress live. `text`-only fields (used solely by `TextEmitter`) are
    dropped from the payload via the emit-site contract: callers pass
    structured fields; nothing text-shaped is passed that shouldn't be
    in the JSON.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def emit(self, event: str, /, **fields: object) -> None:
        payload = {"schema": RUN_SCHEMA, "event": event, **fields}
        self._stream.write(json.dumps(payload) + "\n")
        self._stream.flush()
