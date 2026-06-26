## Tasks

### 1. Pin the lifecycle lines with failing tests first

- [x] In `tests/test_swarm_run.py`, extend the existing happy-path
      success test (the one that drives `_run_single_task` through
      `open_pr` with stub services and already calls
      `capsys.readouterr()`) to assert all four new lines in
      `captured.out`:
  - [x] `worktree: setup <task id>`
  - [x] `worker: running <task id> (timeout <worker_timeout>s)` — pinned
        against the stub services' configured `worker_timeout` so the
        value is exact
  - [x] `commit: <task id>`
  - [x] `push: <task id>`
- [x] In the existing worker-failure test, assert `worker: running
      <task id>` **is** in `captured.out` and that `commit: <task id>`
      and `push: <task id>` are **not** — proving no false success
      markers precede `swarm: <id> failed`.
- [x] Run the suite and confirm these new assertions fail for the right
      reason (lines absent), before touching `_orchestrator.py`.

### 2. Add the four print lines to `_run_single_task`

- [x] `print(f"worktree: setup {task.id}")` immediately after
      `services.worktree.setup(...)` returns.
- [x] `print(f"worker: running {task.id} (timeout
      {services.worker_timeout}s)")` immediately **before**
      `worker.run(invocation)`.
- [x] `print(f"commit: {task.id}")` inside the `try`, immediately after
      `services.git.commit_all_with_bd_export(...)` returns and before
      `push_branch`.
- [x] `print(f"push: {task.id}")` immediately after
      `services.git.push_branch(...)` returns.
- [x] Leave the final `swarm: opened ...` line and all `_handle_failure`
      paths untouched.

### 3. Green + guard the contract

- [x] Run `tests/test_swarm_run.py`; confirm the new assertions pass and
      no previously-passing `capsys` assertion (including the `not in`
      checks) regressed.
- [x] Run the full validation baseline: `uv sync`, `uv run turma init`,
      `uv run turma --help`, `uv run python -m turma --help`,
      `uv run pytest`.

### 4. Docs + changelog

- [x] `docs/architecture.md` Execution section: one sentence noting the
      per-task lifecycle emits `worktree:` / `worker:` / `commit:` /
      `push:` progress lines. State machine diagram unchanged.
- [x] `CHANGELOG.md` `[Unreleased]`: note the additive `turma run`
      per-task progress output.

### 5. Scope guard

- [x] Confirm the diff is limited to: four `print` lines in
      `_orchestrator.py`, test assertions in `test_swarm_run.py`, one
      doc sentence, one changelog line. No adapter, control-flow, or
      state-machine changes. Anything beyond that belongs to a separate
      change.
