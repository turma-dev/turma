# Three-Backend Smoke Checklist

A turnkey pass to confirm the three shipped worker backends — `claude-code`,
`codex`, `opencode` — all drive the *same* real `turma run` workflow to the same
result, differing only in the worker CLI. Run it before a release that touches
backends, after changing a backend, or as a periodic dogfood check.

The swarm pipeline (worktree → worker → sentinel → commit → push → PR →
merge-advancement) is backend-agnostic by design; this checklist verifies that
each backend actually satisfies the worker contract end to end against live
`bd` + `gh`.

## Prerequisites

- The full `turma run` prerequisites — see `docs/smoke-turma-run.md`
  ("Prerequisites"): `bd`, `git`, `gh` (authenticated), and a transcribed
  feature.
- Each backend's CLI on PATH **and authenticated**: `claude`, `codex`,
  `opencode`. (Only the backend you're currently testing is required.)

## Setup (once)

Create one scratch feature and transcribe it, following
`docs/smoke-turma-run.md` → "Scratch setup" + "Pre-populate a transcribed
feature". You will run this single feature once per backend. Re-transcribe with
`turma plan-to-beads --feature <feat> --force` between backends to reset the
task to a fresh `open` state.

## Per-backend run

For each backend, run this full cycle from the scratch repo. `$TURMA_REPO` is
exported in `docs/smoke-turma-run.md` → "Scratch setup"; `uv run` does **not**
work here (the scratch repo has no `pyproject.toml`), so invoke the venv binary
by absolute path:

```bash
BACKEND=<backend>   # claude-code | codex | opencode

# 1. Reset the feature's task to a fresh `open` state
"$TURMA_REPO/.venv/bin/turma" plan-to-beads --feature <feat> --force

# 2. Drive one task end to end
"$TURMA_REPO/.venv/bin/turma" run --feature <feat> --max-tasks 1 --backend "$BACKEND"

# 3. Verify checks 1–4 below, then merge the PR on GitHub (no --delete-branch)

# 4. Re-run to exercise merge-advancement (check 5)
"$TURMA_REPO/.venv/bin/turma" run --feature <feat>
```

Verify all five, for **each** backend:

1. **Autonomous** — the worker runs to completion with no approval prompt or
   hang (within `worker_timeout`).
2. **Worktree edited** — the agent actually changed files under
   `.worktrees/<feat>/<task>/` (not an empty diff).
3. **Sentinel verbatim** — a plain `.task_complete` exists in the worktree
   (the exact filename — no quoting/mangling).
4. **Commit → push → PR** — Turma prints `swarm: opened <id> (PR: <url>;
   awaiting merge)` and the PR exists on GitHub. The worker itself did **not**
   commit (Turma owns the commit boundary).
5. **Merge-advancement** — merge that PR, then re-run
   `turma run --feature <feat>`; the sweep closes the task and cleans the
   worktree (`merge-advancement: <id> → MERGED, closed`).

## Results matrix

| backend | 1. autonomous | 2. worktree edited | 3. `.task_complete` | 4. commit+push+PR | 5. merge-advance closes | notes |
|---|---|---|---|---|---|---|
| `claude-code` |  |  |  |  |  |  |
| `codex` |  |  |  |  |  |  |
| `opencode` |  |  |  |  |  |  |

## Cross-backend consistency

- Text output shape is identical across backends: `fetch:` / `reconcile:` /
  `swarm: claimed` / `worktree: setup` / `worker: running` / `commit:` /
  `push:` / `swarm: opened`. Only the worker CLI invocation differs.
- Optional machine-readable spot-check: re-run one backend with `--json` and
  confirm the `turma.run.v1` NDJSON event stream is well-formed
  (`| jq -c .event`) — it includes `task_opened`, then terminates with
  `stopping_max_tasks` (the `--max-tasks 1` cap; `done` only fires when a run
  reaches no ready work).

## Per-backend notes (what "correct" looks like)

- **`claude-code`** — `claude -p <prompt> --dangerously-skip-permissions`.
- **`codex`** — `codex exec <prompt> --cd <wt> --sandbox workspace-write`
  (least-privilege write sandbox; never `danger-full-access`).
- **`opencode`** — `opencode run <prompt> --dir <wt>
  --dangerously-skip-permissions`. Server/TUI-first, so it spins a local server
  per invocation (heavier start-up). The worker prompt's exact sentinel wording
  is load-bearing here — the shipped `render_worker_prompt` yields a verbatim
  `.task_complete`; do not paraphrase it.

## If a backend fails

Use the failure-signature cheat sheet in `docs/smoke-turma-run.md`. Common
per-backend causes: CLI not on PATH (`error: <name> CLI not found`), the CLI not
authenticated, or a worker that edited nothing (clean tree → `fail_task`). Record
the row's failure in the matrix and triage before treating the backend as
smoke-passing.
