## Scope

One new `WorkerBackend` implementation plus a one-line gate change. `CodexWorker`
mirrors `ClaudeCodeWorker` as closely as the two CLIs allow; everything shared
(prompt, sentinels, timeout handling, registry, service wiring) is reused, not
duplicated. This change proves the `WorkerBackend` abstraction generalizes past
Claude — it does not reshape it.

## The invocation (verified against `codex-cli 0.142.0`)

```
codex exec <prompt> --cd <worktree> --sandbox workspace-write [--model <m>]
```

Run via `subprocess.run(..., capture_output=True, text=True,
timeout=invocation.timeout_seconds)`. Flag rationale, each checked against
`codex exec --help`:

- **`exec <prompt>`** — Codex's non-interactive mode (the same subcommand
  `authoring/codex.py` uses). Prompt is `render_worker_prompt(invocation)` —
  the identical template Claude gets, which instructs the agent to write
  `.task_complete` / `.task_failed`.
- **`--cd <worktree>`** — Codex's explicit "use this directory as the working
  root" flag. `ClaudeCodeWorker` relies on `subprocess(cwd=...)` because the
  `claude` CLI has no such flag; Codex offers `-C/--cd`, which is more explicit
  and avoids any ambiguity about where edits land. (We may still also pass
  `cwd=worktree` for belt-and-suspenders; the smoke will confirm one is
  sufficient.)
- **`--sandbox workspace-write`** — the key difference from authoring, which
  used `--sandbox read-only` because an author only *generates text*. A worker
  *edits its worktree*, so it needs write. `workspace-write` is the
  least-privilege mode that allows that; we deliberately do **not** use
  `danger-full-access`. (Possible values confirmed: `read-only`,
  `workspace-write`, `danger-full-access`.)
- **`--model` omitted (v1)** — Codex uses its configured default, mirroring
  `ClaudeCodeWorker`, which sets no model. A `[swarm]` worker-model knob is
  out of scope (see proposal).
- **No `--output-last-message`** — authoring needs Codex's final message; a
  worker signals via sentinels, so the returned message is irrelevant.
- **`--skip-git-repo-check` omitted** — the task worktree *is* a git repo, so
  the check passes; skipping is unnecessary (authoring skipped it only because
  it ran in a temp dir). The worker must not commit — Turma owns the
  worker-commit boundary — same assumption as the Claude worker.

## The autonomy question — the one thing only a smoke can settle

`codex exec` is non-interactive and the sandbox bounds what shell commands can
do, but we must **confirm against the real CLI** that `exec` +
`workspace-write` proceeds fully autonomously and does not block on an approval
prompt — the equivalent of Claude's `--dangerously-skip-permissions` autonomy.
`--help` shows a `--dangerously-bypass-approvals-and-sandbox` escape hatch and a
`-c key=value` config override, which implies an approval policy exists.

Resolution: a manual smoke against real `codex` drives a scratch worktree task
end to end. If `exec` + `workspace-write` completes and writes the sentinel
without hanging, the argv above is final. If it stalls on approval, add the
minimal non-interactive override (e.g. `-c approval_policy=never` /
`--full-auto`) and re-smoke. This is pinned by smoke rather than a
subprocess-mock unit test on purpose — the same lesson as prior arcs where a
mock validated an argv shape against itself while the real tool behaved
differently (the `fetch` colon-form bug). The unit tests pin the *chosen* argv;
the smoke proves the tool actually obeys it.

## The registry gate

Replace the hardcoded check in `run_swarm`:

```python
# before
if backend is not None and backend != "claude-code":
    raise PlanningError("... v1 registers only 'claude-code'.")

# after
if backend is not None and backend not in registered_worker_backends():
    raise PlanningError(
        f"unknown worker backend: {backend!r}. "
        f"Registered: {list(registered_worker_backends())}"
    )
```

This keeps the fast pre-flight rejection (before any Beads mutation) but sources
truth from the registry. `default_swarm_services` already builds
`worker_factory=lambda: get_worker_backend(backend)`, so once `codex` is in
`_BACKENDS` the worker resolves with no further wiring. `get_worker_backend`
still raises on truly-unknown names as a lazy backstop.

## Naming

Register as **`"codex"`** — it matches the CLI binary and the `--backend codex`
operator ergonomics, paralleling `"claude-code"` (the Claude tool name).

## Tests

Test-first.

- **`tests/test_swarm_worker.py`** (mirror the `ClaudeCodeWorker` suite):
  - `CodexWorker()` raises `PlanningError` when `shutil.which("codex")` is
    `None`.
  - `run(...)` builds the pinned argv (`codex exec <prompt> --cd <worktree>
    --sandbox workspace-write`), injected via a fake `subprocess.run`.
  - `TimeoutExpired` → `WorkerResult(status="timeout", reason=<timeout msg>)`.
  - sentinel dispatch: `.task_complete` → success; `.task_failed` → failure with
    reason; neither → the missing-marker failure — reusing
    `_detect_sentinel_result`, so these mirror the Claude tests exactly.
  - `CodexWorker.name == "codex"` and it appears in
    `registered_worker_backends()`.
- **`tests/test_swarm_run.py`**:
  - `run_swarm(..., backend="codex")` no longer raises at the gate.
  - an unknown backend (e.g. `"nope"`) still raises, and the message names the
    registry rather than the literal `claude-code`.
- **`docs/smoke-*.md`** — a real-`codex` manual smoke: scratch feature, one
  task, `--backend codex`, observe the worktree edited + `.task_complete`
  written + PR opened. This is where the autonomy/approval behavior is actually
  proven.

## Out of items deferred past this change

- OpenCode / Gemini backends.
- `[swarm]` worker-model configuration.
- Worker sandbox/network breadth beyond `workspace-write`.
- Streaming Codex's `--json` event output into a Turma run-events surface
  (relevant to the later machine-readable-run work, not here).
