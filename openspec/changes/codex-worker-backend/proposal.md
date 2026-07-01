## Why

Turma's public differentiation is provider-pool-aware orchestration — spreading
agents across independent provider rate-limit pools. But `turma run` ships only
one worker backend: `claude-code`. `run_swarm` hard-refuses anything else
(`_orchestrator.py`: *"v1 registers only 'claude-code'"*). Until a second real
backend exists, the multi-pool promise is under-delivered.

Codex is the obvious first add:

- Turma already carries Codex provider knowledge on the planning/author side
  (`src/turma/authoring/codex.py` drives `codex exec`), so this validates reuse
  of what we know rather than integrating a cold CLI.
- The worker layer was built for exactly this extension: `WorkerBackend` is a
  two-member protocol (`name`, `run`), completion is a shared sentinel contract
  (`render_worker_prompt` + `_detect_sentinel_result`), and there is a name→
  factory registry (`_BACKENDS` / `get_worker_backend`). The module docstring
  already frames a new backend as "implement the protocol, register the
  factory."

This change adds a `codex` worker backend and unblocks the swarm's hardcoded
single-backend gate.

## What Changes

- **New `CodexWorker`** in `src/turma/swarm/worker.py`, implementing
  `WorkerBackend`, mirroring `ClaudeCodeWorker`:
  - `shutil.which("codex")` check at construction → `PlanningError` with an
    install hint (parallel to `CLAUDE_INSTALL_HINT`).
  - `run(invocation)` drives Codex non-interactively in the task worktree and
    reuses the existing `render_worker_prompt` + `_detect_sentinel_result` — the
    sentinel completion protocol (`.task_complete` / `.task_failed`) is
    unchanged.
  - `subprocess.TimeoutExpired` → `WorkerResult(status="timeout", ...)`, same as
    `ClaudeCodeWorker`.
- **Register it** in `_BACKENDS` under the name `"codex"`.
- **Replace the hardcoded backend gate** in `run_swarm`
  (`backend != "claude-code"` → raise) with a registry-based check against
  `registered_worker_backends()`, so any registered backend is accepted and
  unknown names still fail fast, before any Beads state is mutated.
- **Operator selection** is unchanged in shape: `--backend codex` or
  `[swarm] worker_backend = "codex"`. The default stays `claude-code`.
  `default_swarm_services` already resolves the worker via
  `get_worker_backend(backend)`, so no other wiring changes.

## Capabilities

### Modified Capabilities

- `swarm-orchestration` worker backends: `codex` joins `claude-code` as a
  selectable worker. The swarm no longer hardcodes a single backend name — the
  registry is the source of truth for what `--backend` accepts.

## Impact

- **New files:** none. `CodexWorker` lives in `src/turma/swarm/worker.py`
  beside `ClaudeCodeWorker`.
- **Modified files:**
  - `src/turma/swarm/worker.py` — `CodexWorker` + registry entry + install hint.
  - `src/turma/swarm/_orchestrator.py` — registry-based backend validation
    replacing the `claude-code`-only gate.
  - `tests/test_swarm_worker.py` — `CodexWorker` argv shape, missing-CLI raise,
    timeout, and sentinel dispatch (mirroring the `ClaudeCodeWorker` tests via a
    fake subprocess).
  - `tests/test_swarm_run.py` — `run_swarm` accepts `backend="codex"`; an
    unknown backend still raises, now naming the registry.
  - `README.md` — swarm/backends note that `codex` is selectable.
  - `docs/architecture.md` — worker-backend line (no state-machine change).
  - `docs/smoke-*.md` — a real-`codex` manual smoke (see design; the autonomy
    behavior can only be proven against the real CLI).
  - `CHANGELOG.md` `[Unreleased]`.
- **No new runtime deps.** `codex` is an external CLI prerequisite only when
  selected — exactly like `claude` for `claude-code`.

## Out of Scope

- **OpenCode and Gemini worker backends.** Sequenced after Codex (see
  `agentic-league` surface/backends planning); each is its own change.
- **A per-backend model config knob.** v1 `CodexWorker` uses Codex's configured
  default model, mirroring `ClaudeCodeWorker` (which sets no model). A
  `[swarm]` worker-model setting is a separate later change with a concrete
  need.
- **Parallel / concurrent workers.** The swarm still runs one task at a time.
- **Changing the sentinel completion protocol or the worker prompt.** Both are
  reused as-is; this change proves the abstraction, it does not reshape it.
- **Broader sandbox / network access for workers.** v1 uses the least-privilege
  `workspace-write` sandbox (see design); wider access is a future knob.
