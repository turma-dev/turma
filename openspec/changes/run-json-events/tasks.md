## Tasks

### 1. Pin the event stream with failing tests first

- [ ] In `tests/test_swarm_run.py`, add `--json` / `JsonEmitter` tests driving
      `run_swarm` with a JSON emitter and parsing stdout as NDJSON:
  - [ ] happy single-task loop: assert event order
        (`fetch_advanced → reconcile_summary → task_claimed → worktree_setup →
        worker_running → commit → push → task_opened → done`) and each payload.
  - [ ] every emitted line has `schema == "turma.run.v1"` and an `event` field.
  - [ ] `worker_running` carries `timeout_s`; `task_opened` carries `pr_number`
        + `pr_url`; `task_claimed` carries `task_id` + `title`.
  - [ ] worker-failure/retry: `task_failed` with `attempt` / `max_attempts` /
        `reason` / `budget_exhausted`.
  - [ ] retry-exhaustion halt: `task_failed` then a terminal `error` event, all
        valid NDJSON, process exits nonzero.
  - [ ] dry-run: `fetch_skipped` + reconcile events + merge-advancement
        `dry_run: true`; no claim/commit/push events.
  - [ ] reconcile+repair representative payloads.
  - [ ] **all five merge-advancement variants** (the 1:1 guardrail — none
        dropped): `pr_state` ∈ `MERGED`/`CLOSED`/`OPEN`/`<other>`/`not_found`
        with `action` `closed`/`failed`/`left_alone`/`left_alone_unrecognized`/
        `halting_stale`. The `not_found` case emits `merge_advancement`
        (`halting_stale`) **then** the terminal `error` event and exits nonzero.
- [ ] Confirm the new tests fail for the right reason (no emitter/`--json` yet).

### 2. Introduce the `RunEmitter` seam (text mode = byte-for-byte)

- [ ] Add a `RunEmitter` protocol and `TextEmitter` / `JsonEmitter` (in
      `_orchestrator.py` or a small `swarm/events.py`). `TextEmitter` owns the
      exact current format strings; `JsonEmitter` writes one compact
      `{"schema":"turma.run.v1","event":...,...}` per line and flushes.
- [ ] Add an `emitter: RunEmitter` field to `SwarmServices`, defaulting to
      `TextEmitter()` (after the required fields).
- [ ] Add an `emitter` parameter to `reconcile_feature` (default `TextEmitter()`);
      `run_swarm` passes `services.emitter`.
- [ ] Replace every `print(...)` in `_orchestrator.py` and `reconciliation.py`
      (~18 sites) with `emitter.emit("<event>", **fields)` per the design
      catalog.

### 3. Green the text guard

- [ ] Run `tests/test_swarm_run.py` and `tests/test_swarm_reconciliation.py`;
      **every existing `capsys` text assertion passes with no edits** (the
      byte-for-byte proof). Fix the `TextEmitter` renderings until they do.

### 4. CLI wiring + JSON green

- [ ] `run` subparser in `cli.py` gains `--json` (`action="store_true"`); the
      run branch selects `JsonEmitter` when set and threads it through
      `default_swarm_services`.
- [ ] In `--json` mode, the run branch emits the terminal `error` event (in
      place of `error: <msg>`) on `PlanningError` / `ConfigError`, then exits 1.
- [ ] Run the new `--json` tests green.

### 5. Full baseline

- [ ] `uv sync`, `uv run turma init`, `uv run turma --help`,
      `uv run python -m turma --help`, `uv run pytest`.

### 6. Docs + changelog

- [ ] `README.md`: document `turma run --feature <name> --json` and the
      `turma.run.v1` NDJSON event stream (one object per line, flushed).
- [ ] `docs/architecture.md`: note the run emitter seam + event stream.
- [ ] `CHANGELOG.md` `[Unreleased]`.

### 7. Scope guard

- [ ] Confirm the diff is limited to: the emitter seam + emit sites
      (`_orchestrator.py`, `reconciliation.py`), the `--json` CLI flag, the two
      test files, README/architecture/changelog. No text-output change, no
      `plan` JSON, no worker stdout streaming, no schema-migration framework.
