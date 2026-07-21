# Pooled / Parallel Smoke Checklist

A turnkey pass to confirm the concurrent multi-pool dispatcher drives a **real**
`turma run` across two backends: independent-once-unblocked tasks dispatch
concurrently, route by `turma-type:` to the right pool/backend, and the
`--dry-run` / `--json` / unknown-backend / failed-worker paths stay sane under a
pooled config. Run it after changes to the dispatcher, routing, or config
wiring, or as a periodic dogfood.

The dispatcher core and routing are unit-tested (`tests/test_swarm_parallel.py`,
`tests/test_swarm_run.py`, `tests/test_swarm_cli.py`); this checklist verifies
the same behavior end to end against live `bd` + `gh` + the real `claude` /
`codex` CLIs.

## Prerequisites

- The full `turma run` prerequisites — see `docs/smoke-turma-run.md`
  ("Prerequisites"): `bd`, `git`, `gh` (authenticated), and a scratch repo.
- **Both** pool backends on PATH and authenticated: `claude` and `codex`.
- A disposable GitHub repo you control (reuse your prior `turma-run-smoke-*`).
  If the scratch repo is under a personal account, export a personal
  fine-grained token as `GH_TOKEN` before running — a `gh` session or token
  scoped to a single organization can't open PRs on a personal repo.

## Setup (once)

Follow `docs/smoke-turma-run.md` → "Scratch setup" to export `TURMA_REPO` and
clone the scratch repo, but use **this fixture** instead of hand-authoring a
feature. From the scratch repo root:

```bash
# Pooled config + the convergent-DAG feature (spec root; impl/test/docs deps).
# (the fixture ships as turma.example.toml because a bare turma.toml is gitignored)
cp "$TURMA_REPO/docs/fixtures/pooled-smoke/turma.example.toml" turma.toml
mkdir -p .agents openspec/changes/pooled-smoke .worktrees
cp "$TURMA_REPO/.agents/author.md" "$TURMA_REPO/.agents/critic.md" .agents/
cp "$TURMA_REPO/docs/fixtures/pooled-smoke/"{proposal,design,tasks}.md \
   openspec/changes/pooled-smoke/
touch openspec/changes/pooled-smoke/APPROVED

# Gitignore entries the orchestrator expects. The sentinels are critical:
# the commit boundary stages with `git add -A`, so without these a successful
# task would commit its `.task_complete` sentinel into the PR.
cat >> .gitignore <<'EOF'
.beads/*.db
.worktrees/
.task_complete
.task_failed
.task_progress
EOF

BD_NON_INTERACTIVE=1 bd init --prefix pool    # if not already initialized
bd config set export.interval 0               # required Turma contract

git add -A && git commit -m "pooled smoke: fixture" && git push

# Transcribe tasks.md → Beads, then commit the marker.
"$TURMA_REPO/.venv/bin/turma" plan-to-beads --feature pooled-smoke
git add openspec/changes/pooled-smoke/TRANSCRIBED.md && \
  git commit -m "pooled smoke: transcribed" && git push
```

Confirm the DAG transcribed correctly — four tasks with the four `turma-type:`
labels, and only the `spec` root ready:

```bash
bd list --label feature:pooled-smoke --json --limit 0 \
  | jq -c '.[] | {id, status, type: (.labels | map(select(startswith("turma-type:")))[0])}'
```

Expect four tasks carrying `turma-type:spec|impl|test|docs`. The `spec` root is
the only one ready; the other three are blocked by it (bd may render this as an
`open` task with an active blocker or a distinct `blocked` status depending on
version — the operative check is that **Phase B dispatches exactly one task**).

> **`git restore` before each `turma run`:** any interleaved `bd` command
> re-dirties `.beads/issues.jsonl`, which `turma run`'s bd-state-clean preflight
> refuses. Run this before every `turma run` below (see
> `docs/smoke-three-backends.md` for the full rationale):
>
> ```bash
> git restore --staged --worktree -- .beads/issues.jsonl 2>/dev/null || rm -f .beads/issues.jsonl
> ```

## Phase A — config sanity (no agents, no PRs)

These exercise the pooled wiring without spawning a worker. `T="$TURMA_REPO/.venv/bin/turma"`.

```bash
# A1. Pooled --dry-run: builds the router, runs preflight + reconcile preview,
#     and returns WITHOUT dispatching (no worktrees created, no workers).
"$T" run --feature pooled-smoke --dry-run

# A2. --dry-run --json: a well-formed turma.run.v1 stream ending before dispatch.
"$T" run --feature pooled-smoke --dry-run --json | jq -c .event

# A3. Unknown backend surfaces cleanly (exit 1, one error line — no traceback).
"$T" run --feature pooled-smoke --backend definitely-not-a-backend; echo "exit=$?"

# A4. --max-tasks is refused under a pooled config (no parallel cap semantics)…
"$T" run --feature pooled-smoke --max-tasks 1; echo "exit=$?"
# …but is allowed with --dry-run (the preview exits before dispatch).
"$T" run --feature pooled-smoke --dry-run --max-tasks 1; echo "exit=$?"
```

Expect: A1/A2 reach the reconcile summary and stop (no `worker: running`); A3
prints `error: unknown worker backend: 'definitely-not-a-backend'…` and exits 1;
A4 first invocation exits 1 with the `--max-tasks … parallel execution` message,
second exits 0.

## Phase B — root task (single dispatch)

Only the `spec` root is ready, so this run dispatches one task (to the Anthropic
pool → `claude`) and opens its PR.

```bash
git restore --staged --worktree -- .beads/issues.jsonl 2>/dev/null || rm -f .beads/issues.jsonl
"$T" run --feature pooled-smoke
```

Verify: `swarm: opened <spec-id> (PR: <url>; awaiting merge)`, the PR exists, and
its worktree created `SPEC.txt`. **Merge that PR on GitHub** (no delete-branch),
then continue.

## Phase C — concurrent dependents (the point of the smoke)

Re-running now advances the merged root (closing it), which unblocks `impl`,
`test`, and `docs` **together**. With `max_parallel = 2` they dispatch
concurrently and route by type: `test` → Codex, `impl` / `docs` → Claude. Watch
the live process activity in a second terminal to see both CLIs at once:

```bash
# Terminal 2 (optional, strongest concurrency+routing signal):
watch -n1 'ps -o pid,etime,command -ax | grep -E "[c]laude|[c]odex" | grep -v turma'

# Terminal 1:
git restore --staged --worktree -- .beads/issues.jsonl 2>/dev/null || rm -f .beads/issues.jsonl
"$T" run --feature pooled-smoke --json | tee run.ndjson | jq -c '{event, task_id}'
```

Verify, in order:

1. **Merge-advancement** — the root closes: a `merge_advancement` event with
   `MERGED` / `closed` for the `spec` task.
2. **Concurrency** — two `worker_running` events (for two different `task_id`s)
   appear **before** either's `task_opened`. Cross-check with the process watch:
   a `claude` **and** a `codex` process alive at the same moment.
   ```bash
   # Post-hoc from the captured stream: the max number of tasks whose
   # worker_running precedes their task_opened at any point should be 2.
   jq -c 'select(.event=="worker_running" or .event=="task_opened") | {event, task_id}' run.ndjson
   ```
3. **Routing** — the `test` task ran on Codex and `impl`/`docs` on Claude. The
   run stream carries `task_id`, not backend, so confirm via the process watch
   (a `codex` process appeared) and by the fact the `test` task completed at all
   (only the Codex pool serves `turma-type:test`).
4. **All open** — three more `swarm: opened … (PR: …)` lines / `task_opened`
   events, one per dependent; `IMPL.txt` / `TEST.txt` / `DOCS.txt` each created
   in their worktree; no worker committed (Turma owns the commit boundary).

Merge the three PRs and optionally re-run once more; the sweep closes them and
cleans the worktrees (`merge-advancement: <id> → MERGED, closed`).

## Phase D — failed-worker path stays sane (optional)

Force a deterministic worker failure without a flaky agent by shrinking the
timeout, so workers exceed it and fail cleanly. On a fresh copy of the feature
(re-transcribe with `--force`), set `worker_timeout = 5` in `turma.toml`, then
run Phase C again:

- Each worker fails with a `timeout` result → `swarm: <id> failed …` /
  `task_failed` (`budget_exhausted` once retries run out), the task is labelled
  `needs_human_review`, and the run exits non-zero — **no crash, no hang**.
- With `max_parallel = 2`, a sibling already in flight when another exhausts its
  budget still reaches its terminal before the run raises (drain-not-cancel).
  This is exercised deterministically by `test_failure_halt_drains_in_flight_not_cancel`;
  the live check just confirms the failure surface is clean.

Restore `worker_timeout` afterward.

## Results checklist

| # | check | pass? |
|---|---|---|
| A1 | pooled `--dry-run` reaches reconcile, does not dispatch | |
| A2 | `--dry-run --json` stream well-formed | |
| A3 | unknown backend → exit 1, single error line | |
| A4 | `--max-tasks` refused (pooled), allowed under `--dry-run` | |
| B | root (`spec`) dispatches to Claude, PR opens | |
| C1 | merge-advancement closes the merged root | |
| C2 | two workers run concurrently (`claude` + `codex` alive together) | |
| C3 | `test` routed to Codex, `impl`/`docs` to Claude | |
| C4 | all three dependent PRs open, worktrees edited, Turma owns commits | |
| D | failed-worker path fails cleanly + drains (optional) | |

## If something fails

Use the failure-signature cheat sheet in `docs/smoke-turma-run.md`. Pooled-run
specifics:

- **Only one worker ever runs at a time** — check `max_parallel > 1` in
  `turma.toml` and that the dependents actually unblocked (`bd list` shows them
  `open`, not `blocked`); dependents unblock only after the root's PR is
  **merged** and a subsequent run's merge-advancement closes it.
- **`test` task fails to start / wrong backend** — confirm `codex` is on PATH and
  authenticated, and that the `openai` pool declares `types = ["test"]`.
- **bd-state-clean preflight refuses** — run the `git restore` line above before
  the `turma run`.
