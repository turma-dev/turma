## Why

`turma run` narrates its progress as a stream of text lines (`reconcile:`,
`repair:`, `swarm: claimed`, `worktree: setup`, `worker: running`, `commit:`,
`push:`, `swarm: opened` / `failed` / `done`). That is fine for a human at a
terminal but unusable for a surface — a VS Code extension, an MCP client, a
dashboard — that wants to render live swarm progress. Such a caller today would
have to scrape and pattern-match the text, which is brittle and was never a
contract.

`run` is **operational telemetry, not state inspection.** Unlike `turma status`
(a single snapshot, already `--json`-able as `turma.status.v1`), a run is a
sequence of events over time. A single end-of-run JSON blob would help
after-the-fact scripts but would not solve the live-surface problem. The right
shape is a **newline-delimited JSON (NDJSON) event stream**: one compact JSON
object per line, emitted and flushed at each transition, so a caller can consume
progress as it happens. This mirrors how `codex` and `opencode` themselves
expose `--format json`.

This change delivers that surface for `run`. It is the remaining half of the
surface-exploration promotion trigger (two non-Claude backends already ship);
the machine-readable surface is what unblocks an eventual GUI/MCP cockpit.

## What Changes

- **New flag:** `turma run --feature <name> --json` emits an NDJSON event
  stream to stdout instead of the text lines. Consistent with `turma status
  --json`. Absent the flag, the text output is **byte-for-byte unchanged**.
- **An emitter seam replaces scattered `print(...)`.** The orchestrator's ~18
  output sites (in `_orchestrator.py` and `reconciliation.py`) currently call
  `print(...)` directly. They move behind a `RunEmitter` passed through
  `SwarmServices` (and into `reconcile_feature`, which does not receive
  `services` today). Two implementations:
  - `TextEmitter` — renders each event as today's exact text line(s),
    byte-for-byte. The existing `capsys` text tests are the guard.
  - `JsonEmitter` — writes one `{"schema": "turma.run.v1", "event": "...", ...}`
    object per line to stdout and **flushes after each event**.
- **A stable event envelope:** every event is a JSON object with `schema`
  (`"turma.run.v1"`) and `event` (the transition name), plus event-specific
  fields. One event type per existing text transition, 1:1 where practical.
- **CLI wiring:** `--json` selects `JsonEmitter`; the run branch of `cli.py`
  threads it through `default_swarm_services`.

## Guardrails (explicit)

- **One JSON object per existing text transition, where practical.** The event
  catalog is pinned to today's output lines; this change does not invent new
  telemetry, only re-encodes what already prints.
- **Flush after each event** so streaming UI/MCP callers see progress live.
- **No worker stdout/stderr streaming.** Announcing `worker_running` is in
  scope; surfacing the worker's own output is not.
- **No `plan` JSON.** `plan`'s machine-readable surface is a separate change
  (different shape — state/gate transitions, not a run stream).
- **No schema-migration framework** beyond the `schema` string. A single stable
  identifier now; versioning machinery is a future decision.
- **Failure behavior preserved.** Errors still exit nonzero. If an error occurs
  mid-run, the events already emitted remain valid, complete NDJSON records
  (they are flushed). A terminal failure is emitted as an event **only where a
  corresponding text line exists today** — `task_failed` for the orchestrator's
  `swarm: <id> failed …` line, and a terminal `error` event in place of the
  CLI's `error: <msg>` line.

## Capabilities

### Modified Capabilities

- `swarm-orchestration` operator output: `run` gains an NDJSON `--json` event
  stream alongside the text output, via an emitter seam. The text rendering is
  unchanged; JSON is a second emitter over the same transitions.

## Impact

- **New files:** none (the emitter can live in `_orchestrator.py` or a small
  `swarm/events.py` — implementation choice).
- **Modified files:**
  - `src/turma/swarm/_orchestrator.py` — `RunEmitter` on `SwarmServices`;
    ~18 `print(...)` sites become `emitter.emit(...)`.
  - `src/turma/swarm/reconciliation.py` — `reconcile_feature` takes an emitter;
    its `reconcile:` prints become emits.
  - `src/turma/cli.py` — `run` subparser gains `--json`; wires the emitter.
  - `tests/test_swarm_run.py` — new `--json` tests asserting event order +
    payloads for representative paths; existing `capsys` text tests stay
    **unchanged** (the byte-for-byte guard).
  - `tests/test_swarm_reconciliation.py` — reconcile emitter plumbing.
  - `README.md`, `docs/architecture.md` — document `run --json` / the event
    stream. `CHANGELOG.md` `[Unreleased]`.
- **No new runtime deps.** `json` is stdlib.

## Out of Scope

- `plan` JSON (separate change).
- Worker stdout/stderr streaming.
- Schema versioning/negotiation machinery.
- Any change to the text output, its lines, or their order.
- New telemetry beyond the existing text transitions.
- `status`-style single-snapshot JSON for `run` (the stream is the contract).
