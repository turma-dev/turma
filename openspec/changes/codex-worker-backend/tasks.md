## Tasks

### 1. Pin `CodexWorker` behavior with failing tests first

- [x] In `tests/test_swarm_worker.py`, mirror the `ClaudeCodeWorker` suite for a
      new `CodexWorker` (inject a fake `subprocess.run` / patch `shutil.which`):
  - [x] constructor raises `PlanningError` when `shutil.which("codex")` is None.
  - [x] `run(...)` invokes the pinned argv: `codex exec <rendered prompt>
        --cd <worktree> --sandbox workspace-write` (assert the exact list).
  - [x] `subprocess.TimeoutExpired` → `WorkerResult(status="timeout")` with the
        shared timeout reason.
  - [x] sentinel dispatch via `_detect_sentinel_result`: `.task_complete` →
        success; `.task_failed` → failure with the file's reason; neither →
        missing-marker failure.
  - [x] `CodexWorker.name == "codex"`, and `"codex"` is in
        `registered_worker_backends()`.
- [x] In `tests/test_swarm_run.py`: `run_swarm(..., backend="codex")` does not
      raise at the gate; an unknown backend still raises and the message names
      the registry.
- [x] Confirm these fail for the right reason before touching `worker.py` /
      `_orchestrator.py`.

### 2. Implement `CodexWorker`

- [x] Add `CODEX_INSTALL_HINT` and a `CodexWorker` class in
      `src/turma/swarm/worker.py` beside `ClaudeCodeWorker`: `name = "codex"`,
      `shutil.which("codex")` check in `__init__`, `run` driving the argv above
      with `capture_output=True, text=True, timeout=...`, `TimeoutExpired` →
      timeout result, else `_detect_sentinel_result`.
- [x] Register `"codex": CodexWorker` in `_BACKENDS`.

### 3. Registry-based backend gate

- [x] In `src/turma/swarm/_orchestrator.py`, replace the
      `backend != "claude-code"` check in `run_swarm` with membership in
      `registered_worker_backends()` (fast pre-flight; unknown names still
      raise, message names the registry). Import the helper.

### 4. Green + full baseline

- [x] Run `tests/test_swarm_worker.py` and `tests/test_swarm_run.py`; new tests
      pass, nothing regressed.
- [x] Full validation baseline: `uv sync`, `uv run turma init`,
      `uv run turma --help`, `uv run python -m turma --help`, `uv run pytest`.

### 5. Manual smoke against real `codex` (the autonomy proof)

- [~] Smoke doc step added (`docs/smoke-turma-run.md` "Codex worker backend").
      Autonomy confirmed by an **isolated probe** (codex-cli 0.142.0): `codex
      exec "<task>" --cd <dir> --sandbox workspace-write` created files and
      wrote `.task_complete` autonomously, exit 0, no approval prompt. The full
      end-to-end `turma run --backend codex` against live `bd` + `gh` (opening a
      real PR) is left as an operator-run smoke — flagged as such in the doc.
- [x] No approval stall observed in the probe, so no override flag was needed;
      the pinned argv (`--sandbox workspace-write`, no bypass) stands unchanged.

### 6. Docs + changelog

- [x] `README.md`: note `codex` is a selectable `--backend` / `[swarm]
      worker_backend`, with its `codex` CLI prerequisite.
- [x] `docs/architecture.md`: worker-backend line reflects two backends (no
      state-machine change).
- [x] `CHANGELOG.md` `[Unreleased]`.

### 7. Scope guard

- [x] Confirm the diff is limited to: `worker.py` (`CodexWorker` + registry),
      `_orchestrator.py` (gate), the two test files, the smoke doc, README,
      architecture, changelog. No config-schema change, no OpenCode/Gemini, no
      prompt/sentinel change, no worker-model knob.
