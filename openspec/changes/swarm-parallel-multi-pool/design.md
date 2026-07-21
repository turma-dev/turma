## Scope

Turn `run_swarm` from a sequential single-backend loop into a bounded concurrent
dispatcher that routes tasks by type across capped provider pools, with every
shared-state mutation serialized behind one lock, and extend `run.v1` with the
stream identity a concurrent consumer needs. The hard part is not parallelism
itself — it is doing it without racing on the two shared resources the swarm
already depends on: the single Beads/Dolt database and the parent repo's `.git`.

## Current sequential state (what we are replacing)

`_main_loop` (`_orchestrator.py`) runs one task at a time:

```
while slots-remain:
    ready = beads.list_ready_tasks(feature)   # bd read
    task  = ready[0]
    beads.claim_task(task.id)                  # bd write (atomic)
    _run_single_task(task)                     # worktree add → worker → commit → push → PR
```

`_run_single_task` sets up a git worktree, runs the one configured backend,
commits, pushes, opens the PR, and marks the task. `--backend` picks that single
backend for the whole run. Nothing overlaps.

**What is already concurrency-safe and must not be rebuilt:**

- **Atomic claim.** `claim_task` is `bd update <id> --claim` (atomic in Beads); a
  lost race raises and the loop already skips it (`claim_race`, budget not
  consumed). Two slots can call `list_ready_tasks` and pick the same task — only
  one claim wins; the loser re-fetches. Concurrency does not reopen this.
- **Reconciliation** already walks the full in-progress set, so it tolerates N
  concurrent in-progress tasks with no change to its classification logic.
- **Merge gate.** A dependent unblocks only when its blocker is `closed`, and a
  task closes only at merge-advancement (`MERGED`). So even under concurrency no
  dependent is claimable until its upstream merges — pre-merge-dependent
  execution stays out of scope.

## Concurrency model

- **Bounded slots.** A dispatcher keeps up to `MAX_PARALLEL` **top-level** worker
  tasks in flight, refilling from `list_ready_tasks` as slots free. `MAX_PARALLEL`
  counts top-level slots only; backends may spawn their own subagents (Claude
  Code especially — plan for 2–4× actual sessions), invisible here. Caps are
  documented as needing conservative sizing, not modeled.
- **Primitive: in-process `asyncio`.** Each slot is an `asyncio` task; the worker
  subprocess is awaited via `asyncio.create_subprocess_exec`. No server, no
  thread pool required for the I/O-bound worker waits. (A hosted/durable-execution
  engine is a separate, later track; v1 is local-first and in-process.)
- **Per-pool gating.** Each pool has an `asyncio.Semaphore(cap)`; a task acquires
  its pool's semaphore before a slot starts, so per-pool caps hold and total
  in-flight = min(`MAX_PARALLEL`, Σ pool caps). Independent pools draw from
  independent provider ceilings — the additive-throughput point.

## Pool configuration & routing (decision: `[[swarm.pools]]`)

Pools are a TOML array-of-tables in `turma.toml`:

```toml
[[swarm.pools]]
name     = "anthropic"
backend  = "claude-code"
types    = ["impl", "spec"]   # turma-type: labels routed here
max      = 2
default  = true               # exactly one pool must set this; unmatched types route here

[[swarm.pools]]
name     = "openai"
backend  = "codex"
types    = ["test", "docs"]
max      = 2
```

- **Routing key: the existing `turma-type:` label.** A ready task's type selects
  its pool. **A type not matched by any pool's `types` routes to the default
  pool** — the pool marked `default = true`. Config-load requires **exactly one**
  `default = true` pool whenever `[[swarm.pools]]` is present (the back-compat
  implicit pool is the default), so an unmatched type always has a home and never
  errors at run time. Each pool binds one backend from the existing worker
  registry.
- **Back-compat.** With no `[[swarm.pools]]` block, behavior is today's:
  one implicit pool over all types using `[swarm].worker_backend`, `max = 1` (or
  a documented default). `--backend <id>` remains a single-pool override for the
  whole run (all types → that backend), preserving the current CLI contract.
- `SwarmConfig` gains the parsed pool list; `cli.py`'s run branch resolves the
  router from it.

## Serialized shared-state critical section (decision: one global lock)

Worker *execution* is concurrent; **all shared-state mutation is serialized**
behind a single global orchestration mutation lock. Two shared resources force
this, and both are easy to under-scope:

1. **The Beads/Dolt database.** *Every* `BeadsAdapter` subprocess call hits the
   one Dolt DB, whose writes take a lock and whose export/pre-commit-hook
   machinery has proven fragile even under sequential use. This is not just the
   obvious writers (`claim_task`, `mark_pr_open`, `unmark_pr_open`, `close_task`,
   `fail_task`, `create_task`, `export`) — the reads go to the same DB too
   (`list_ready_tasks`, `list_in_progress_tasks`, `get_task_body`,
   `retries_so_far`, `list_feature_tasks*`). **The v1 rule is: every
   `BeadsAdapter` subprocess call runs under the lock unless it is proven
   read-only-and-safe** — so no lock-taking `bd` command is accidentally left
   outside. Proving specific reads safe (to run them lock-free for throughput) is
   a later optimization.
2. **The parent repo's `.git`.** `WorktreeManager.setup` runs
   `git -C <repo_root> worktree add <path> -b <branch> <base>` and cleanup runs
   worktree-remove + branch-delete against the **shared** parent repo, mutating
   `.git/worktrees/` and creating/deleting refs. Concurrent invocations race on
   git's index/ref locks. Worktrees isolate *files*, not `.git` metadata — so
   worktree add/removal and branch create/delete also run under the lock.

**One lock, not two, in v1.** A single global mutation lock covers both the Dolt
DB and `.git` metadata. This trades some throughput (the serialized section is
short relative to the worker wait, which stays concurrent) for a simple, provably
race-free boundary. The worker subprocess itself — the long pole — runs *outside*
the lock. Splitting into separate Dolt/`.git` locks, or a bd-write queue, is a
named future optimization, not v1.

The `export → git add → commit` worker-commit sequence and the
`push` are part of the per-task tail; the bd/`.git`-touching steps of that tail
hold the lock, the network `push` need not.

## Failure semantics (decision: exhausted budget halts the run)

- **Retry-budget exhaustion halts the whole run**, matching today's `_main_loop`
  (which raises `PlanningError` on exhaustion). v1 does **not** add per-pool
  quiescing.
- **Stop protocol (decision: drain, do not cancel).** On a halting failure the
  dispatcher **stops scheduling new slots** but lets every in-flight worker reach
  its **normal terminal handling** — commit → push → open PR, or `fail_task` —
  rather than cancelling it. Cancelling mid-tail would strand edited worktrees
  and written sentinels in an ambiguous state; draining leaves each task at a
  clean terminal that the next run's reconciliation already understands
  (`completion-pending`, `pr_open`, released-to-`open`, etc.). Once all in-flight
  slots have drained, the run exits nonzero. (`--max-tasks` reaching its cap is
  the same drain path minus the nonzero exit.) Per-pool isolation ("halt only
  that pool, keep the others") is explicitly deferred.
- Non-halting task failures (retry budget remaining) release the task back to
  `open` exactly as today; another slot may re-claim it later.
- The merge gate and reconciliation are unchanged, so crash-recovery semantics
  carry over: a re-run reconciles N in-progress tasks the same way.

## `run.v1` event changes (stream identity for concurrent consumers)

Sequential `run.v1` events need no identity because they never interleave. Under
concurrency, events from N slots are interleaved on one stdout, so a consumer
must be able to (a) group a run, (b) order events, and (c) know when a task is
truly done. Additive changes (existing events keep their fields):

- **`run_id`** — a per-invocation UUID (`uuid4`), included on every event
  envelope: `{"schema":"turma.run.v1","run_id":<uuid>,"event":…,"ts":…,…}`.
- **`ts`** — a wall-clock ISO-8601 timestamp on every event, so a consumer can
  order interleaved events and compute durations. (Wall-clock, not monotonic:
  the value is for display/correlation, not internal scheduling.)
- **`started`** — a new first event carrying `run_id`, `feature`, the resolved
  pools/caps, and `max_parallel`, so a cockpit can lay out lanes before work
  begins.
- **`completed`** — a new single per-task completion event. Today "task done" is
  split across two `run` invocations (`task_opened` = PR open + still
  in_progress, then a later `merge_advancement` `closed`), which a live cockpit
  cannot stitch within one run. `completed` fires when a slot finishes its tail
  (PR opened / failed / released), carrying `task_id` + terminal `outcome`, so a
  lane can close without correlating across runs.
- **`heartbeat`** — a periodic event during the `worker_running` wait
  (`task_id`, elapsed), so a long silent worker window does not read as a hang.

`--backend`/text mode keep working; text rendering ignores the new envelope
fields (the `TextEmitter` seam already owns per-event formatting).

## Tests

Test-first, concurrency-invariant-focused (the risky part is races, not the JSON):

- **Serialization invariant (the load-bearing test).** With a fake `BeadsAdapter`
  and a fake worktree manager that record call order and assert non-overlap, run
  the dispatcher with `MAX_PARALLEL > 1` and prove that **no two `bd` subprocess
  calls and no two `git worktree`/branch ops ever overlap** — i.e. the global
  lock actually serializes both resources. This is written to fail before the
  lock exists.
- **Concurrency actually happens.** Fake workers with controllable durations;
  assert ≥2 workers are in their (lock-free) execution window simultaneously, and
  that total in-flight respects `MAX_PARALLEL` and each pool's `Semaphore(cap)`.
- **Routing.** `turma-type:` → pool → backend selection; default-pool fallback;
  `--backend` single-pool override; empty/absent `[[swarm.pools]]` = today's
  behavior.
- **Failure halts the run.** Retry-budget exhaustion in one slot stops new
  scheduling and exits nonzero, in-flight slots resolved to a safe boundary.
- **`run.v1` additions.** Every event carries `run_id` + `ts`; `started` first,
  `completed` per task with `outcome`, `heartbeat` during the worker wait;
  interleaved events from concurrent slots are each valid NDJSON and groupable by
  `run_id` + `task_id`.
- **Reconciliation under concurrency.** A re-run with N in-progress tasks
  classifies/repairs them unchanged.

## Deferred past this change

- Finer-grained locking (separate Dolt/`.git` locks; a bd-write queue; proving
  reads lock-free).
- Per-pool failure isolation / quiescing.
- Dynamic autoscaling and cross-provider load balancing.
- Pre-merge-dependent / integration-branch execution.
- A hosted/durable-execution orchestrator.
- Any UI/MCP/scheduler surface consuming the enriched stream.
