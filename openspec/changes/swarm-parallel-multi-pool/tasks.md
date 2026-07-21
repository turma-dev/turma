## Tasks

### 1. Pin the concurrency + serialization invariants with failing tests first

The risk in this change is *races*, not the feature surface, so the invariants
are pinned before any implementation.

- [ ] **Serialization invariant (load-bearing).** With a fake `BeadsAdapter` and
      fake `WorktreeManager` that record every subprocess call with enter/exit
      markers, drive the dispatcher at `MAX_PARALLEL > 1` and assert **no two
      `bd` calls overlap and no two `git worktree`/branch ops overlap** — the
      global lock serializes both shared resources. Fails before the lock exists.
- [ ] **Concurrency actually happens.** Fake workers with controllable durations;
      assert ≥2 workers are in their (lock-free) execution window at once, total
      in-flight ≤ `MAX_PARALLEL`, and each pool never exceeds its `Semaphore(cap)`.
- [ ] **Routing.** `turma-type:` → pool → backend selection; default-pool
      fallback for an unmatched type; `--backend` single-pool override; absent
      `[[swarm.pools]]` reproduces today's single-backend behavior.
- [ ] **`run.v1` identity.** Every event carries `run_id` (stable within a run) +
      `ts`; `started` is first; `completed` fires once per task with `outcome`;
      `heartbeat` appears during the worker wait; interleaved events are each
      valid NDJSON and groupable by `run_id` + `task_id`.
- [ ] **Failure halts the run.** Retry-budget exhaustion in one slot stops new
      scheduling and the run exits nonzero.
- [ ] Confirm all of the above fail for the right reason (no dispatcher/lock/
      router/identity yet).

### 2. Pool config + routing

- [ ] `[[swarm.pools]]` array-of-tables in `turma.example.toml` (name, backend,
      types, max) and `SwarmConfig` parsing in `src/turma/config.py`.
- [ ] A pool-registry/router: `turma-type:` label → pool → backend (from the
      existing worker registry) + cap. Decide and implement the unmatched-type
      policy (default pool vs config-load error) and document it.
- [ ] Back-compat: no `[[swarm.pools]]` ⇒ one implicit pool over all types using
      `[swarm].worker_backend`; `--backend <id>` overrides to a single pool for
      the whole run.
- [ ] Green the routing tests from Task 1.

### 3. Concurrent dispatcher + per-pool caps

- [ ] Replace the sequential body of `_main_loop` (`_orchestrator.py`) with a
      bounded `asyncio` dispatcher: keep up to `MAX_PARALLEL` top-level slots in
      flight, refill from `list_ready_tasks` as slots free, each slot awaiting its
      worker subprocess via `asyncio.create_subprocess_exec`.
- [ ] Per-pool `asyncio.Semaphore(cap)` acquired before a slot starts; total
      in-flight = min(`MAX_PARALLEL`, Σ caps).
- [ ] Preserve the existing claim-race handling (a lost `claim_task` skips,
      budget not consumed) unchanged.
- [ ] Green the concurrency-happens + cap tests from Task 1.

### 4. Global mutation lock (Beads DB + shared `.git`)

- [ ] Introduce one global orchestration mutation lock. **Every `BeadsAdapter`
      subprocess call runs under it unless proven read-only-and-safe** — writers
      (`claim`/`mark_pr_open`/`unmark_pr_open`/`close_task`/`fail_task`/
      `create_task`/`export`) and the DB-reading calls
      (`list_ready_tasks`/`list_in_progress_tasks`/`get_task_body`/
      `retries_so_far`/`list_feature_tasks*`).
- [ ] The same lock guards shared-`.git` metadata ops: `WorktreeManager.setup`
      (`git worktree add … -b <branch>`) and cleanup (worktree remove + branch
      delete). The worker subprocess and the network `push` stay **outside** the
      lock.
- [ ] Green the serialization-invariant test from Task 1 (the load-bearing one).

### 5. `run.v1` stream identity

- [ ] `src/turma/swarm/events.py`: add `run_id` (per-invocation `uuid4`) and `ts`
      (wall-clock ISO-8601) to every event envelope; existing events keep their
      fields.
- [ ] New events: `started` (run_id, feature, resolved pools/caps, max_parallel),
      `completed` (task_id, outcome — the single per-task terminal a live cockpit
      needs), and `heartbeat` (task_id, elapsed) during the worker wait.
- [ ] Confirm `TextEmitter` ignores the new envelope fields (text output for the
      existing events is unchanged); green the identity tests from Task 1.

### 6. Reconciliation under concurrency + failure-halt

- [ ] Verify reconciliation/repair and merge-advancement behave with N concurrent
      in-progress tasks (classification is already set-based); add a test.
- [ ] Failure-halt: retry-budget exhaustion stops scheduling new slots; in-flight
      slots resolve to a safe boundary (or cancel) per the stop protocol; run
      exits nonzero as today. No per-pool quiescing. Green the failure test.

### 7. Full baseline + docs + scope guard

- [ ] `uv sync`, `uv run turma init`, `uv run turma --help`,
      `uv run python -m turma --help`, `uv run pytest` all green.
- [ ] Docs: `README.md` (pools config + concurrent run + the `run.v1` additions),
      `docs/architecture.md` (concurrent dispatcher + one-lock critical section +
      routing), `turma.example.toml` (`[[swarm.pools]]`), `CHANGELOG.md`
      `[Unreleased]`.
- [ ] Scope guard: confirm the diff is limited to the dispatcher/router/lock,
      `[[swarm.pools]]` config, the `run.v1` identity additions, and their tests
      + docs. **No** UI/MCP/scheduler surface, **no** finer-grained locking, **no**
      per-pool failure isolation, **no** change to the merge gate or
      pre-merge-dependent behavior.
