## Tasks

### 1. Pin `OpenCodeWorker` behavior with failing tests first

- [x] In `tests/test_swarm_worker.py`, mirror the Codex suite for a new
      `OpenCodeWorker` (fake `subprocess.run` / patch `shutil.which`):
  - [x] constructor raises `PlanningError` when `shutil.which("opencode")` is
        None; install-hint wording asserted.
  - [x] `run(...)` invokes the pinned argv: `opencode run <rendered prompt>
        --dir <worktree> --dangerously-skip-permissions` (assert exact list).
  - [x] `subprocess.TimeoutExpired` → `WorkerResult(status="timeout")`.
  - [x] sentinel dispatch via `_detect_sentinel_result`: complete / failed /
        missing.
  - [x] `OpenCodeWorker.name == "opencode"`; `get_worker_backend("opencode")`
        returns an instance.
  - [x] grow the registry assertion to
        `("claude-code", "codex", "opencode")`.
- [x] In `tests/test_swarm_run.py`: `run_swarm(..., backend="opencode")` passes
      the gate (fails at preflight, not "unknown worker backend"), mirroring the
      Codex acceptance test.
- [x] Confirm these fail for the right reason before touching `worker.py`.

### 2. Implement `OpenCodeWorker`

- [x] Add `OPENCODE_INSTALL_HINT` and an `OpenCodeWorker` class in
      `src/turma/swarm/worker.py`: `name = "opencode"`, `shutil.which("opencode")`
      check, `run` = render prompt → build argv → `_run_cli_worker(argv,
      invocation)`.
- [x] Register `"opencode": OpenCodeWorker` in `_BACKENDS`.
- [x] No `_orchestrator.py` change — the gate is already registry-based.

### 3. Green + full baseline

- [x] Run `tests/test_swarm_worker.py` and `tests/test_swarm_run.py`; new tests
      pass, nothing regressed.
- [x] Full validation baseline: `uv sync`, `uv run turma init`,
      `uv run turma --help`, `uv run python -m turma --help`, `uv run pytest`.

### 4. Manual smoke against real `opencode`

- [x] Extend `docs/smoke-turma-run.md`: `turma run --backend opencode`.
      Verify (or record from the isolated probe) that `opencode run` +
      `--dangerously-skip-permissions` edits the worktree and writes a
      **verbatim** `.task_complete` using the real rendered worker prompt.
- [x] Full end-to-end `turma run --backend opencode` against live `bd` + `gh`
      (opening a real PR) is operator-run; note it as such.

### 5. Docs + changelog

- [x] `README.md`: `opencode` selectable `--backend` / `[swarm]
      worker_backend`, with its `opencode` CLI prerequisite. Narrow the
      "not yet available" list to Gemini.
- [x] `docs/architecture.md`: worker-backends line reflects three backends;
      deferred list narrows to Gemini.
- [x] `turma.example.toml`: registered names now include `opencode`.
- [x] `CHANGELOG.md` `[Unreleased]`.

### 6. Scope guard

- [x] Confirm the diff is limited to: `worker.py` (`OpenCodeWorker` + registry),
      the two test files, the smoke doc, README, architecture, `turma.example.toml`,
      changelog. No `_orchestrator.py` change, no Gemini, no prompt/sentinel
      change, no worker-model knob.
