## Scope

The third worker backend, and the smallest yet: one `WorkerBackend`
implementation, one registry entry, no orchestrator change. `OpenCodeWorker`
plugs a third argv into the shared `_run_cli_worker` seam the Codex arc created.
This change is the abstraction's real test — OpenCode is a materially different
CLI shape than `claude -p` / `codex exec`, and the point is to show the worker
contract still fits.

## The invocation (verified against `opencode 1.14.28`)

```
opencode run <prompt> --dir <worktree> --dangerously-skip-permissions
```

Run via the shared `_run_cli_worker` (`subprocess.run(..., cwd=worktree,
capture_output=True, text=True, timeout=...)`, then timeout / sentinel
dispatch). Flag rationale, each checked against `opencode run --help`:

- **`run <prompt>`** — OpenCode's non-interactive subcommand (the same one
  `authoring/opencode.py` uses). Prompt is `render_worker_prompt(invocation)`,
  identical to the other workers.
- **`--dir <worktree>`** — OpenCode's explicit "directory to run in" flag.
  OpenCode is server-based, so it takes the working directory as a flag rather
  than relying solely on `cwd`; this is the parallel to Codex's `--cd`. (We
  still pass `cwd=worktree` via the shared helper as well.)
- **`--dangerously-skip-permissions`** — auto-approves OpenCode's permission
  prompts. This is the autonomy flag, a direct parallel to Claude's identically-
  named flag, and it is required: without it a worker would block on OpenCode's
  interactive permission gate.
- **`--model` omitted (v1)** — OpenCode uses its configured default
  (`provider/model`), parity with the other workers. A `[swarm]` model knob is
  out of scope.
- **No `--format json`** — the worker signals via sentinels; the event stream is
  irrelevant here (it belongs to the later run-events work).

`OpenCodeWorker.run` is therefore just: render prompt → build argv →
`return _run_cli_worker(argv, invocation)`.

## What the "different CLI shape" proves — and the one gotcha

OpenCode is server / TUI-first: `opencode run` spins a local server per
invocation, which is heavier than `claude -p` or `codex exec`. The isolated
probe (below) still completed within budget, and the subprocess + timeout model
handles it unchanged. That is exactly the diversity picking OpenCode second is
meant to exercise: the worker contract — "a subprocess that edits the worktree
and writes a sentinel" — is surface-agnostic, and OpenCode satisfies it through
a different door.

**Sentinel-filename de-risk (a real probe finding).** An ad-hoc probe prompt
(hand-written, not the template) made OpenCode create the sentinel as
`` `.task_complete` `` — with literal backticks — which `_detect_sentinel_result`
would miss. Re-probing with the **actual** `render_worker_prompt` output
produced a verbatim `.task_complete`. Conclusion: the shared prompt works with
OpenCode as-is, but the prompt's exact wording is load-bearing for OpenCode's
filename handling. The implementation smoke must confirm verbatim sentinel
creation using the real rendered prompt, not a paraphrase.

## No orchestrator change

`run_swarm`'s gate is already `backend not in registered_worker_backends()`
(from `codex-worker-backend`), and `default_swarm_services` builds
`worker_factory=lambda: get_worker_backend(backend)`. Registering `"opencode"`
in `_BACKENDS` makes `--backend opencode` work end to end with no other wiring.

## Naming

Register as **`"opencode"`** — matches the CLI binary and `--backend opencode`,
paralleling `"claude-code"` / `"codex"`.

## Tests

Test-first, mirroring the Codex suite.

- **`tests/test_swarm_worker.py`**:
  - `OpenCodeWorker()` raises `PlanningError` when `shutil.which("opencode")` is
    `None`; install-hint wording.
  - `run(...)` builds the pinned argv (`opencode run <prompt> --dir <worktree>
    --dangerously-skip-permissions`) via a fake `subprocess.run`.
  - `TimeoutExpired` → `WorkerResult(status="timeout")`.
  - sentinel dispatch (complete / failed / missing) through
    `_detect_sentinel_result`.
  - `OpenCodeWorker.name == "opencode"`; `get_worker_backend("opencode")`
    returns an instance.
  - the registry assertion grows to `("claude-code", "codex", "opencode")`.
- **`tests/test_swarm_run.py`**: `run_swarm(..., backend="opencode")` passes the
  gate (fails later at preflight, not "unknown worker backend"), mirroring the
  Codex acceptance test. The unknown-backend test already asserts the registry
  is named.
- **`docs/smoke-turma-run.md`**: OpenCode note + the autonomy/verbatim-sentinel
  probe result; full end-to-end `turma run --backend opencode` left operator-run.

## Autonomy — verified by probe

`opencode 1.14.28`, isolated temp dir, using the **real rendered worker
prompt**: `opencode run "<prompt>" --dir <dir> --dangerously-skip-permissions`
→ exit 0, edited files, wrote a verbatim `.task_complete`, no approval prompt.
So the argv above is final; no permission/config override beyond
`--dangerously-skip-permissions` is needed.

## Out of items deferred past this change

- Gemini worker backend (the sequence's third).
- `[swarm]` worker-model configuration.
- OpenCode `--format json` event streaming.
- Worker concurrency.
