## Scope

Add run identity (`run_id`), per-event time (`ts`), run lifecycle bookends
(`run_started` / `run_completed`), and a `heartbeat` keepalive to the
`turma.run.v1` JSON stream, and make the emitter thread-safe. Text output is
unchanged. Consumes the deferred Task 5 of `swarm-parallel-multi-pool`.

## Current state

`src/turma/swarm/events.py`: `RunEmitter` protocol (`emit(event, /, **fields)`),
`TextEmitter` (renders historical lines via `_render_text`), `JsonEmitter`
(`payload = {"schema": RUN_SCHEMA, "event": event, **fields}` → `write` +
`flush`, unlocked). The orchestrator/dispatcher call `services.emitter.emit(...)`
at each transition; under the concurrent dispatcher these calls come from several
worker threads. The CLI builds `JsonEmitter()` for `--json` and passes it as
`services.emitter`.

## Decisions

### `run_id` — per-invocation UUID, on every JSON event

A `uuid4` hex string, generated **in `JsonEmitter.__init__`** (one emitter == one
`turma run` invocation) and injected into every payload. `JsonEmitter` accepts an
optional `run_id` argument so tests can pin it. `TextEmitter` ignores it (text is
unchanged). Rationale for emitter-owned (vs orchestrator-owned): the emitter
stamps *every* event including ones the orchestrator doesn't originate (CLI
`error`, lifecycle), and it keeps `run_swarm`'s signature untouched.

The CLI's single `JsonEmitter` is also the one passed as `services.emitter`, so
the CLI's lifecycle events and the orchestrator/dispatcher's detail events share
one emitter instance → **one `run_id` across the whole run**.

### `ts` — ISO-8601 UTC, stamped at emit time

`datetime.now(timezone.utc).isoformat()` (microsecond precision, `+00:00`
offset), added in `JsonEmitter.emit` at the moment of emission — so `ts` reflects
when the event *happened*, and interleaved concurrent events are orderable by
real time rather than stream position. JSON-only.

### Lifecycle bookends — `run_started` / `run_completed` (CLI-owned)

The CLI `run` handler owns the envelope — **not `run_swarm`** — so it covers
failures *before* `run_swarm` (config load, services construction) as well as
`run_swarm`'s own paths. This closes the gap where a `load_swarm_config()` failure
would otherwise emit only `error` (via `_run_error`) with no lifecycle bookends.

The envelope is **active only in `--json` mode** (decision 3). The CLI emits both
events through **the emitter it built for `--json`** — the `JsonEmitter` itself,
*not* `services.emitter`: there is no `services` (hence no `services.emitter`) yet
when config or service construction fails, which is exactly the case the envelope
must cover. In text mode none of this runs (see "Text mode").

- **`run_started`** — emitted once config **and** services are built, immediately
  before `run_swarm`. Fields: `feature`, `dry_run`, `mode` (`"sequential"` |
  `"concurrent"`), `max_parallel`, and either `backend` (sequential) or `pools`
  (concurrent: `[{name, backend, max}]`). If config load or services construction
  fails, **no `run_started` fires** — nothing started.
- **`run_completed`** — emitted in a `finally` wrapping the whole handler, so it
  fires on **every** terminal path, including pre-run config/services errors.
  Fields: `outcome` and `duration_ms` (see below). Complements — does not replace
  — the existing detail events (`done`, `stopping_max_tasks`, `error`).

**Contract:** exactly one `run_completed` per invocation; `run_started` iff the
run actually started. A config-error stream is therefore `error` +
`run_completed(outcome="error")` with **no** `run_started` — which correctly
signals "failed before the run started."

### Outcome — the state carrier (`SwarmHalted` typed exception)

The CLI derives `outcome` from **how the handler exited**, so no run-summary
object has to be threaded through the execution path for v1:

- `run_swarm` returns normally → `outcome = "completed"` (covers done / dry-run /
  stopped-at-max-tasks — all "ended normally").
- `run_swarm` raises **`SwarmHalted(PlanningError)`** — a new typed subclass
  raised where the retry budget is exhausted, replacing today's bare
  `PlanningError` on that path → `outcome = "halted"`.
- config/services construction fails, or `run_swarm` raises any other
  `PlanningError` / exception → `outcome = "error"` (the existing `error` event
  still fires alongside).

`duration_ms` is CLI-held: a monotonic clock captured at **handler entry** (so
even pre-run failures get a duration), read in the `finally`.

**Deferred enrichment (out of v1 scope):** finer outcomes (`done` vs `dry_run` vs
`stopped_max_tasks`) and task counts (`tasks_opened` / `tasks_failed`) need a
`RunSummary` carrier populated at the `task_opened` / `task_failed` sites across
both execution paths and returned from `run_swarm` (or carried on `SwarmHalted`).
Defined here so the extension point is known; v1 `run_completed` is `outcome` +
`duration_ms` only.

### `heartbeat` — background keepalive (CLI-owned)

A daemon ticker the CLI starts after `run_started` and stops in the same
`finally` before `run_completed`. It emits `heartbeat` (field: `elapsed_ms`) every
`interval` seconds until signalled (a `threading.Event`; the wait *is* the
interval, so stop is immediate). Rationale for a thread: the long silence is a
single blocking `worker.run` (up to `worker_timeout`), so no natural in-loop emit
point fires during it — only a timer keeps the stream alive.

Default interval: **15s**, configurable via `[swarm].heartbeat_interval` (`0`
disables) — decision 2. The ticker runs **only in `--json` mode** (decision 3),
so the default human/text path spawns no extra thread. (`in_flight` worker count
is dispatcher-internal and not exposed to the CLI-owned ticker in v1.)

### Thread-safe emitter — line-atomic and synchronous (not async)

`JsonEmitter.emit` wraps `write` + `flush` in a `threading.Lock` so concurrent
worker-thread events and the heartbeat can't interleave into a corrupt line —
this also fixes a latent unlocked-concurrent-emit bug in the shipped dispatcher.
`TextEmitter` gets the same lock — **not just symmetry**: the concurrent
dispatcher emits task events from worker threads in text mode too (a pooled
`turma run` *without* `--json`), so its `print()` calls must not interleave either.

Emit is **line-atomic and synchronous** — deliberately *not* asynchronous or
non-blocking. A pathologically slow consumer stream can backpressure through the
lock and stall the worker-thread emits (and thus the run). That is acceptable for
a CLI run stream — the consumer is normally a fast pipe or the operator's
terminal, and it matches today's `write`+`flush` behavior. A queued writer with a
drop/backpressure policy, to fully decouple the run from a slow consumer, is
**deferred** until a real consumer needs it.

### Text mode — truly untouched

Because lifecycle + heartbeat are `--json`-gated (decision 3), the CLI runs the
core config→services→`run_swarm` sequence **bare** in text mode — no lifecycle
events, no heartbeat thread. This is stronger than "events dropped by the
renderer": the default human path gets **no hidden concurrency and no new
output**. `TextEmitter` therefore never receives `run_started` / `run_completed` /
`heartbeat`, and `_render_text` is unchanged (it still raises on a genuinely
unknown event — a correctness guard). The byte-for-byte text assertions in
`tests/test_swarm_run.py` hold unchanged.

## Ordering / correctness under concurrency

- Consumers order by `ts`, correlate by `run_id` (+ `task_id` for per-task
  events). Stream position is not authoritative once workers interleave.
- `run_started` is guaranteed first and `run_completed` last *in wall-clock*; a
  `heartbeat` may interleave anywhere between. That's fine — they're identified
  by `event`, not position.

## Tests

- Emitter unit tests: `JsonEmitter` — `run_id` present + stable across a run;
  `ts` present + ISO-8601; N concurrent threads produce N well-formed lines (no
  interleave) under the lock. `TextEmitter` — N concurrent `print()`s produce N
  intact lines under its lock.
- Text-mode gating: `turma run` without `--json` emits **no** `run_started` /
  `run_completed` / `heartbeat` and starts **no** ticker thread.
- CLI `run --json` lifecycle tests: `run_started` first with the right
  mode/fields; `run_completed` last with `outcome` ∈ {`completed`, `halted`,
  `error`} on the normal / `SwarmHalted` / other-exception paths; a **config-load
  failure** emits `error` + `run_completed(outcome="error")` with **no**
  `run_started`; every event carries `run_id` + `ts`.
- Text byte-for-byte: existing text assertions still pass unchanged.
- Heartbeat: fires at least once across a run longer than one interval (inject a
  small interval + a slow stub worker); stops promptly at run end.

## Deferred / out of scope

- The MCP/VS Code consumer of the stream.
- Run persistence / history.

## Resolved decisions

1. **Lifecycle event names → `run_started` / `run_completed`** (the `run_`
   prefix). Clearer in a stream that already has task-level events, and ages
   well once there are task/worker/PR/phase lifecycle events too.
2. **Heartbeat interval → `[swarm].heartbeat_interval`, default 15s, `0`
   disables.** Configurable now: cadence is environment-sensitive (local
   terminal vs CI vs MCP bridge vs editor panel vs hosted runner), and
   hard-coding would guarantee a follow-up.
3. **Lifecycle + heartbeat are `--json`-gated.** The CLI runs the envelope and
   ticker only when `--json` is active; the default text path stays truly
   untouched — no hidden concurrency, no dropped-by-renderer events. `run_id` /
   `ts` are naturally JSON-only (injected by `JsonEmitter`; `TextEmitter` never
   sees them).
