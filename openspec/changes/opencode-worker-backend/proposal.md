## Why

Codex shipped as the first non-Claude worker backend. OpenCode is the agreed
second — and it is chosen deliberately: it exercises the `WorkerBackend`
abstraction against a **different agent CLI shape** (OpenCode is a server / TUI-
first tool whose non-interactive mode is an `opencode run` subcommand), not just
another hosted-model provider. If the abstraction holds for OpenCode, it is not
merely "Claude plus Codex."

The path is contained:

- Turma already carries OpenCode provider knowledge on the planning/author side
  (`src/turma/authoring/opencode.py` drives `opencode run`).
- The Codex arc left a shared `_run_cli_worker` helper (subprocess + timeout +
  sentinel detection); the two existing workers differ only in argv. A third
  worker is: build argv, register.
- The `run_swarm` backend gate is already registry-based (from
  `codex-worker-backend`), so no orchestrator change is needed — registering the
  backend is sufficient to make `--backend opencode` selectable.

## What Changes

- **New `OpenCodeWorker`** in `src/turma/swarm/worker.py`, implementing
  `WorkerBackend`, mirroring the existing workers:
  - `shutil.which("opencode")` check at construction → `PlanningError` with an
    install hint.
  - `run(invocation)` builds `opencode run <prompt> --dir <worktree>
    --dangerously-skip-permissions` and delegates to the shared
    `_run_cli_worker` — reusing `render_worker_prompt` and
    `_detect_sentinel_result` unchanged.
- **Register** it in `_BACKENDS` under the name `"opencode"`.
- **No `_orchestrator.py` change.** The backend gate already validates against
  `registered_worker_backends()`, and `default_swarm_services` already resolves
  the worker via `get_worker_backend(backend)`.
- **Operator selection:** `--backend opencode` or `[swarm] worker_backend =
  "opencode"`. Default stays `claude-code`.

## Capabilities

### Modified Capabilities

- `swarm-orchestration` worker backends: `opencode` joins `claude-code` and
  `codex` as a selectable worker. Three backends now share one subprocess /
  timeout / sentinel path, differing only in argv.

## Impact

- **New files:** none. `OpenCodeWorker` lives in `src/turma/swarm/worker.py`.
- **Modified files:**
  - `src/turma/swarm/worker.py` — `OpenCodeWorker` + registry entry + install
    hint.
  - `tests/test_swarm_worker.py` — `OpenCodeWorker` argv / missing-CLI /
    timeout / sentinel dispatch / name; the registered-backends assertion grows
    to `("claude-code", "codex", "opencode")`.
  - `tests/test_swarm_run.py` — `run_swarm(..., backend="opencode")` is accepted
    past the gate (mirrors the Codex acceptance test).
  - `README.md`, `docs/architecture.md`, `turma.example.toml` — note `opencode`
    is selectable; narrow the "not yet available" list to Gemini.
  - `docs/smoke-turma-run.md` — OpenCode note + the autonomy probe result.
  - `CHANGELOG.md` `[Unreleased]`.
- **No new runtime deps.** `opencode` is an external CLI prerequisite only when
  selected, like `claude` and `codex`.

## Out of Scope

- **Gemini worker backend.** The third and last in the agreed sequence; its own
  change (and expected to surface different auth/model assumptions).
- **A `[swarm]` worker-model knob.** v1 `OpenCodeWorker` uses OpenCode's
  configured default model, parity with the other workers.
- **Streaming OpenCode's `--format json` event output** into a Turma run-events
  surface — relevant to the later machine-readable-run work, not here.
- **Parallel / concurrent workers.** One task at a time, unchanged.
- **Changing the shared worker prompt or sentinel protocol.** Reused as-is (and
  the probe confirms OpenCode honors it — see design).
