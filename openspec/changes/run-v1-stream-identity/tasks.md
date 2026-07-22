## Tasks

Decisions (from review): (1) names `run_started` / `run_completed`;
(2) `[swarm].heartbeat_interval`, default 15s, `0` disables; (3) lifecycle +
heartbeat are `--json`-gated (text mode runs neither).

### 1. Failing tests first
- [ ] Emitter tests: `JsonEmitter` injects a stable `run_id` + ISO-8601 `ts` into
  every event, and N concurrent threads emit N well-formed NDJSON lines (no
  interleave) under the lock; `TextEmitter` — N concurrent `print()`s stay intact
  under its lock.
- [ ] CLI `run --json` lifecycle tests: `run_started` first (correct mode +
  fields); `run_completed` last with `outcome` ∈ {completed, halted, error};
  config-load failure → `error` + `run_completed(error)` with **no**
  `run_started`; every event carries `run_id` + `ts`.
- [ ] CLI text-mode gating test: `turma run` (no `--json`) emits no `run_started`
  / `run_completed` / `heartbeat` and starts no ticker thread; text byte-for-byte
  assertions unchanged.

### 2. Identity + thread-safety in the emitters
- [ ] `JsonEmitter(run_id=None)` — generate a `uuid4` hex if not given; inject
  `run_id` + a fresh `ts` into every payload.
- [ ] Wrap `write` + `flush` in a `threading.Lock` (both emitters — the
  dispatcher emits from worker threads in text mode too). `_render_text` is
  unchanged (lifecycle/heartbeat are `--json`-gated, so it never sees them).

### 3. Lifecycle bookends (CLI-owned, `--json`-gated)
- [ ] Add `SwarmHalted(PlanningError)`; `run_swarm` raises it on retry exhaustion
  (replacing the bare `PlanningError` on that path).
- [ ] CLI `run` handler, **only when `--json`**: capture a monotonic clock at
  handler entry; emit `run_started` (feature, dry_run, mode, max_parallel,
  backend/pools) once config **and** services are built; in a `finally`, emit
  `run_completed` with `outcome` (`completed` on return, `halted` on
  `SwarmHalted`, `error` otherwise — incl. config/service-construction failures) +
  `duration_ms`, via the CLI's `JsonEmitter` (no `services.emitter` on pre-run
  failure). One `run_completed` per invocation; `run_started` only if the run
  started. Text path runs the core sequence bare (as today).

### 4. Heartbeat ticker (CLI-owned, `--json`-gated)
- [ ] `[swarm].heartbeat_interval` config (default 15s, `0` disables) parsed in
  `SwarmConfig`.
- [ ] Daemon thread emitting `heartbeat` (`elapsed_ms`) every interval (stop via a
  `threading.Event`; the wait is the interval so stop is immediate). CLI starts it
  after `run_started`, stops it in the same `finally`, **only in `--json` mode**.

### 5. Docs + baseline
- [ ] Update `run-json-events` event catalog / `docs/` (`architecture.md` JSON
  surface, any `--json` examples) with `run_id` / `ts` / lifecycle / heartbeat.
- [ ] `turma.example.toml`: document `heartbeat_interval`.
- [ ] CHANGELOG; full baseline green; scope guard.
