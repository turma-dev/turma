## Why

`turma run` is Turma's differentiator on paper — routing work across independent
provider rate-limit pools so total swarm concurrency is *additive* rather than
capped by any one provider's ceiling — but the shipped orchestrator does not do
it. `run_swarm`'s `_main_loop` (`src/turma/swarm/_orchestrator.py`) is a
**sequential** loop: it claims one ready task, drives it end to end, then claims
the next, and `--backend` selects a **single** worker for the whole run. So the
product runs one task at a time against one provider — the opposite of the claim.

Two prerequisites are now in place. The three worker backends
(`claude-code`, `codex`, `opencode`) are proven to satisfy the same worker
contract end to end, and `run --json` (`turma.run.v1`) exists as the machine
surface a concurrent cockpit would consume. This change builds the actual
concurrency: **concurrent worker slots, routed by task type across provider
pools, each pool capped independently.** It is the load-bearing product feature;
UI/distribution surfaces stay deferred behind it.

## What Changes

- **Concurrent dispatch.** `_main_loop` becomes a bounded concurrent dispatcher:
  up to `MAX_PARALLEL` **top-level** worker slots run at once, refilled from
  `list_ready_tasks` as slots free. Beads' atomic claim already guarantees no two
  slots grab the same task (`claim_task` + the existing `claim_race` skip), so
  concurrency does not reopen claim-safety.
- **Task-type → provider-pool routing.** A task's `turma-type:` label selects a
  **pool**; each pool is bound to a worker backend and its provider's rate-limit
  ceiling. This replaces the single run-wide `--backend`.
- **Per-pool concurrency caps.** Each pool has its own slot cap, so no pool
  starves the others; effective concurrency is the sum across pools — the
  additive-throughput thesis.
- **Serialized shared-state critical section.** Worker *execution* runs
  concurrently, but all shared-state mutations — every `BeadsAdapter` subprocess
  call (one Dolt DB, lock-taking) and every shared-`.git` metadata operation
  (`git worktree add`/removal, branch create/delete against the parent repo) —
  run under **one global orchestration mutation lock**. Designed in, not
  discovered as a race.
- **`run.v1` stream identity for concurrent consumers.** The event stream gains a
  per-invocation `run_id`, per-event ISO-8601 timestamps, a `started` event, a
  single task-**completed** event, and a periodic worker `heartbeat`, so a
  cockpit can multiplex interleaved events from N slots into per-task lanes.
- **`[[swarm.pools]]` config.** Pools (name, backend, task types, cap) are
  declared as a TOML array-of-tables in `turma.toml`; a default preserves
  today's single-backend behavior.

## Guardrails (explicit)

- **Moat before surface.** This change ships the differentiator. MCP / VS Code /
  scheduler / any UI surface stays **out of scope and deferred behind it** — the
  point is that the concurrent multi-pool swarm exists before anything renders it.
- **One global mutation lock in v1.** Beads-DB access *and* shared-`.git`
  metadata ops serialize behind a single lock. Finer-grained locking (separate
  Dolt vs `.git`, or proving individual `bd` reads lock-free) is a later
  optimization, not v1.
- **Failure semantics unchanged.** An exhausted retry budget still **halts the
  whole run** (matching today's `_main_loop`); v1 does not add per-pool
  quiescing. In-flight slots are allowed to finish or are cancelled per the
  design's stop protocol, but no new "keep going on other pools" behavior.
- **Merge gate unchanged.** Dependents still unblock only at `merged`, so no
  dependent runs against an unmerged upstream even under concurrency. No
  pre-merge-dependent / integration-branch mode.
- **`MAX_PARALLEL` counts top-level slots only.** Backends may spawn their own
  subagents (2–4× actual sessions), invisible to the orchestrator; caps are
  documented as needing conservative sizing, not modeled.

## Capabilities

### Modified Capabilities

- `swarm-orchestration` execution model: `run` changes from a sequential
  single-backend loop to a bounded concurrent dispatcher that routes tasks by
  type across capped provider pools, with all shared-state mutations serialized
  behind one lock.
- `swarm-orchestration` operator output: `run.v1` gains `run_id`, per-event
  timestamps, a `started` event, a task-`completed` event, and a `heartbeat`
  event (additive; existing events keep their fields).

## Impact

- **New files:**
  - a pool-registry / router module (task-type → pool → backend + cap).
  - a concurrent dispatcher (bounded slots + the global mutation lock), replacing
    the sequential body of `_main_loop`.
- **Modified files:**
  - `src/turma/swarm/_orchestrator.py` — sequential loop → concurrent dispatcher;
    mutation lock around Beads + worktree ops.
  - `src/turma/swarm/worktree.py` — worktree add/removal + branch ops invoked
    under the lock.
  - `src/turma/swarm/beads.py` — `BeadsAdapter` calls invoked under the lock
    (no per-method change; the caller holds the lock).
  - `src/turma/swarm/events.py` — `run_id`, timestamps, `started`, `completed`,
    `heartbeat`.
  - `src/turma/config.py`, `turma.example.toml` — `[[swarm.pools]]`.
  - `src/turma/cli.py` — `run` reads pools; `--backend` becomes a single-pool
    override.
  - tests under `tests/test_swarm_*.py`; `README.md`, `docs/architecture.md`,
    `CHANGELOG.md`.
- **New runtime deps:** none anticipated (`asyncio`, `uuid`, `datetime`,
  `threading`/`asyncio` locks are stdlib). Confirmed during implementation.

## Out of Scope

- Any UI/MCP/VS Code/scheduler surface (deferred behind this change).
- Dynamic autoscaling or cross-provider load-balancing beyond static per-pool
  caps.
- Pre-merge-dependent / integration-branch execution (the merge gate serializes
  dependents already).
- A hosted/managed orchestrator or durable-execution engine (v1 is local-first,
  plain in-process).
- Finer-grained locking than the single global mutation lock.
- Worker stdout/stderr streaming.
