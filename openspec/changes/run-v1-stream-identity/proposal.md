## Why

`turma run --json` emits a `turma.run.v1` NDJSON stream (`run-json-events`), but
the stream is **anonymous and untimed**: an event is
`{"schema":"turma.run.v1","event":"worker_running","task_id":"smoke-pxs"}` — no
run identifier, no timestamp, no run lifecycle. That was tolerable for the
sequential loop (one event at a time, order == stream order). It is not, now that
the concurrent multi-pool dispatcher (`swarm-parallel-multi-pool`) ships: events
from several worker threads **interleave**, so a consumer (VS Code / MCP /
dashboard) can't correlate them to a run, order them by real time, or tell a live
run from a hung one during the long silent stretch of `worker.run` (up to
`worker_timeout`, e.g. 30 min).

This is the explicitly-deferred **Task 5 of `swarm-parallel-multi-pool`**
("run.v1 stream identity for concurrent consumers").

A correctness bug rides along: the dispatcher already emits from worker threads
concurrently, but `JsonEmitter.emit` does an unlocked `write` + `flush`, so two
threads' NDJSON lines can interleave into corrupt output. Adding a heartbeat
(another emitting thread) makes this unavoidable, so the emitter is made
thread-safe as part of this change.

## What Changes

Give the `turma.run.v1` JSON stream **identity, time, and lifecycle** so it is
machine-consumable under concurrency — the prerequisite for any live surface
built on it.

- **`run_id`** — a per-invocation UUID on **every** JSON event, so interleaved
  events are correlatable to one run.
- **`ts`** — an ISO-8601 UTC timestamp on every JSON event, stamped at emit time,
  so consumers order by real time rather than stream position.
- **Run lifecycle events** — `run_started` (feature, dry-run, execution mode +
  `max_parallel`, backend/pools) and `run_completed` (`outcome` ∈ {`completed`,
  `halted`, `error`}, `duration_ms`). **CLI-owned and `--json`-gated**, so they
  bookend the whole invocation including pre-run config failures; `run_completed`
  fires on every terminal path. (Finer outcomes + task counts are a defined,
  deferred enrichment.)
- **`heartbeat`** — a periodic keepalive (interval `[swarm].heartbeat_interval`,
  default 15s, `0` disables) emitted by a background ticker **in `--json` mode**,
  so a consumer can distinguish a live run from a stalled one during long silent
  worker execution.
- **Thread-safe emitter** — a lock around `emit` so concurrent worker-thread
  events and the heartbeat never interleave into corrupt NDJSON (also fixes a
  latent bug: the shipped dispatcher already emits from worker threads unlocked,
  in text mode too).

## Guardrails (explicit)

- **Text mode is truly untouched.** `run_id` / `ts` are JSON-only, and lifecycle
  + heartbeat are `--json`-gated — so the default human path runs **no extra
  thread and emits no extra events** (not "events dropped by the renderer"). The
  existing `TextEmitter` assertions hold unchanged.
- **The existing event set and field shapes are additive-only.** `run_id` / `ts`
  are added to existing payloads; no existing event is renamed or removed. A
  consumer written against `run-json-events` keeps working.
- **Emit is line-atomic and synchronous, not asynchronous.** The lock around
  `write` + `flush` keeps concurrent NDJSON well-formed; it does **not** decouple
  the run from a slow consumer — a pathologically slow stream can backpressure the
  run through the lock. Acceptable for a CLI stream (fast pipe / terminal), and it
  matches today's behavior; a queued/drop writer to fully decouple is deferred
  until a real consumer needs it.

## Capabilities

### Modified Capabilities

- **`turma run --json` stream (`run-json-events`)** — every event gains `run_id`
  + `ts`; the stream gains `run_started` / `run_completed` bookends and periodic
  `heartbeat`. `turma.run.v1` schema string is unchanged (additive).

## Impact

- `src/turma/swarm/events.py` — `JsonEmitter` carries `run_id`, stamps `ts`,
  becomes thread-safe; `TextEmitter` gets thread-safe writes for concurrent task
  events; lifecycle/heartbeat are CLI JSON-only (neither emitter renders them).
- `src/turma/cli.py` — the `run` handler owns the lifecycle envelope
  (`run_started` / `run_completed` in a `finally`, covering pre-run config/service
  failures) and the heartbeat ticker; maps the exit path → `outcome`.
- `src/turma/swarm/_orchestrator.py` + errors — retry-exhaustion halt raises a
  new typed `SwarmHalted(PlanningError)` so the CLI can tell halted from error.
- Tests: `tests/test_swarm_cli.py` (CLI lifecycle + identity + config-error
  envelope), `tests/test_swarm_run.py` (text byte-for-byte preserved), plus
  emitter unit tests (thread-safety, ts/run_id injection).

## Out of Scope

- The MCP server / VS Code surface that consumes this stream (this is the
  enabling plumbing, not the surface).
- Any change to the `turma.plan.v1` / `turma.status.v1` schemas.
- Persisting runs / a run history store — this is a live stream, not storage.
