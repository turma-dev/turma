## Tasks

### 1. Pin `CodexWorker` behavior with failing tests first

- [ ] In `tests/test_swarm_worker.py`, mirror the `ClaudeCodeWorker` suite for a
      new `CodexWorker` (inject a fake `subprocess.run` / patch `shutil.which`):
  - [ ] constructor raises `PlanningError` when `shutil.which("codex")` is None.
  - [ ] `run(...)` invokes the pinned argv: `codex exec <rendered prompt>
        --cd <worktree> --sandbox workspace-write` (assert the exact list).
  - [ ] `subprocess.TimeoutExpired` → `WorkerResult(status="timeout")` with the
        shared timeout reason.
  - [ ] sentinel dispatch via `_detect_sentinel_result`: `.task_complete` →
        success; `.task_failed` → failure with the file's reason; neither →
        missing-marker failure.
  - [ ] `CodexWorker.name == "codex"`, and `"codex"` is in
        `registered_worker_backends()`.
- [ ] In `tests/test_swarm_run.py`: `run_swarm(..., backend="codex")` does not
      raise at the gate; an unknown backend still raises and the message names
      the registry.
- [ ] Confirm these fail for the right reason before touching `worker.py` /
      `_orchestrator.py`.

### 2. Implement `CodexWorker`

- [ ] Add `CODEX_INSTALL_HINT` and a `CodexWorker` class in
      `src/turma/swarm/worker.py` beside `ClaudeCodeWorker`: `name = "codex"`,
      `shutil.which("codex")` check in `__init__`, `run` driving the argv above
      with `capture_output=True, text=True, timeout=...`, `TimeoutExpired` →
      timeout result, else `_detect_sentinel_result`.
- [ ] Register `"codex": CodexWorker` in `_BACKENDS`.

### 3. Registry-based backend gate

- [ ] In `src/turma/swarm/_orchestrator.py`, replace the
      `backend != "claude-code"` check in `run_swarm` with membership in
      `registered_worker_backends()` (fast pre-flight; unknown names still
      raise, message names the registry). Import the helper.

### 4. Green + full baseline

- [ ] Run `tests/test_swarm_worker.py` and `tests/test_swarm_run.py`; new tests
      pass, nothing regressed.
- [ ] Full validation baseline: `uv sync`, `uv run turma init`,
      `uv run turma --help`, `uv run python -m turma --help`, `uv run pytest`.

### 5. Manual smoke against real `codex` (the autonomy proof)

- [ ] Add a `docs/smoke-*.md` step (or extend the run smoke): scratch feature,
      one task, `turma run --backend codex`. Verify the worktree is actually
      edited, `.task_complete` is written, and a PR opens — i.e. `codex exec`
      + `workspace-write` runs autonomously without an approval prompt.
- [ ] If it stalls on approval, add the minimal non-interactive override to the
      argv (e.g. `-c approval_policy=never` / `--full-auto`), update the pinned
      argv test in Task 1, and re-smoke. Record the final argv in the smoke doc.

### 6. Docs + changelog

- [ ] `README.md`: note `codex` is a selectable `--backend` / `[swarm]
      worker_backend`, with its `codex` CLI prerequisite.
- [ ] `docs/architecture.md`: worker-backend line reflects two backends (no
      state-machine change).
- [ ] `CHANGELOG.md` `[Unreleased]`.

### 7. Scope guard

- [ ] Confirm the diff is limited to: `worker.py` (`CodexWorker` + registry),
      `_orchestrator.py` (gate), the two test files, the smoke doc, README,
      architecture, changelog. No config-schema change, no OpenCode/Gemini, no
      prompt/sentinel change, no worker-model knob.
