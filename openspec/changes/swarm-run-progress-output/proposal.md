## Why

`turma run` drives one claimed task through
`worktree.setup → worker.run → commit → push → open_pr` inside
`_run_single_task` (`src/turma/swarm/_orchestrator.py`), and prints
**nothing** between `swarm: claimed <id> — <title>` and the final
`swarm: opened <id> (PR: <url>; awaiting merge)`.

`worker.run` is the long pole: the worker agent runs for up to
`services.worker_timeout` (default 1800s). During that single longest
wait the operator stares at a silent terminal with no signal of which
step is active or whether the worker is even alive.

Every other phase of the run already emits a compact, prefixed line —
`fetch:`, `reconcile:`, `repair:`, `merge-advancement:`, `swarm:`. The
per-task lifecycle is the one gap, and it sits exactly where the wait is
longest. This change closes that gap and nothing else.

## What Changes

Add four additive, deterministic, text-only stdout lines inside the
success path of `_run_single_task`, in execution order:

- `worktree: setup <id>` — after `services.worktree.setup(...)` returns
- `worker: running <id> (timeout <N>s)` — **before** `worker.run(...)`,
  where `<N>` is `services.worker_timeout`; this is the line that tells
  the operator what the long silence is
- `commit: <id>` — after `commit_all_with_bd_export(...)` succeeds
- `push: <id>` — after `push_branch(...)` succeeds

The existing final line — `swarm: opened <id> (PR: <url>; awaiting
merge)` — is unchanged.

`<id>` is `task.id` throughout, matching the existing `swarm: claimed
<id>` line. Lines are emitted with `print(...)` to stdout, consistent
with the rest of the orchestrator. No timestamps, no durations — the
only interpolated values are the task id and the (config-fixed) timeout,
so output is deterministic for a given run.

Failure paths are **not** touched: the worker-failure, clean-tree, and
push/PR-error branches already speak through
`swarm: <id> failed (...)` in `_handle_failure`. No lifecycle markers
are added there in this change.

## Capabilities

### Modified Capabilities

- `swarm-orchestration` operator output: the per-task lifecycle gains
  `worktree:` / `worker:` / `commit:` / `push:` progress lines. Purely
  additive to the existing compact per-task output style described in
  `openspec/changes/swarm-orchestration/design.md` ("prints a compact
  per-task summary as the loop progresses"). No existing line changes
  format or placement.

## Impact

- **New files:** none. The four `print(...)` calls live in
  `_run_single_task` in `src/turma/swarm/_orchestrator.py`.
- **Modified files:**
  - `src/turma/swarm/_orchestrator.py` — four lifecycle `print` lines in
    the success path of `_run_single_task`.
  - `tests/test_swarm_run.py` — `capsys` assertions pinning each new
    line on the happy-path success test; a negative check that the
    lifecycle lines do **not** appear on the worker-failure path.
  - `docs/architecture.md` — Execution section notes the per-task
    lifecycle output (one short sentence; the state machine is
    unchanged).
  - `CHANGELOG.md` `[Unreleased]`.
- **No new runtime deps.** No adapter changes, no new argv, no state
  machine change.

## Out of Scope

- `worker: complete <id>` / `worker: failed <id>` lines. Success already
  speaks through `swarm: opened ...`; failure through
  `swarm: <id> failed ...`. Adding completion markers would touch the
  failure-path test surface for marginal value. Deferred unless a later
  change needs them.
- `preflight: ok` and any other phase-walk markers (preflight, fetch
  framing on dry-run, reconcile/repair "nothing to do" lines). Success
  preflight silence is fine and failures already explain themselves;
  broad phase-walk output risks output-contract churn for little gain.
- Timestamps, elapsed durations, spinners, progress bars.
- Streaming the worker's own stdout/stderr line-by-line. This change
  announces that the worker is running; it does not surface its output.
- Structured / JSON output modes.
- Parallel or multi-worker execution. `turma run` still processes one
  ready task at a time.
