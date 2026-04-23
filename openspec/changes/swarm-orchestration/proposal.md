## Why

`turma plan` produces approved specs and `turma plan-to-beads` translates
them into a feature-tagged Beads task DAG, but nothing executes those
tasks. An approved plan sits as a set of open Beads issues and an
`APPROVED` marker on disk, and the operator still has to claim,
implement, and PR each task by hand. This change adds `turma run` as
the first executable loop on top of that DAG — a small, sequential,
single-feature orchestrator that claims one ready Beads task at a time,
runs a configured worker agent in an isolated git worktree, and opens
a PR when the work lands.

Deliberately v1 scope. Parallel execution, multi-backend routing
optimization, provider-pool scheduling, retry policies beyond a simple
per-task budget, cross-feature concurrency, and any commercial
packaging are all out of scope here. The goal is a usable end-to-end
execution path that the next iteration can harden and expand.

## What Changes

- New `turma run --feature <name>` subcommand that drives the claim →
  work → PR loop for a single approved feature until no ready tasks
  remain, a budget is hit, or a task fails.
- `BeadsAdapter` gains `list_ready_tasks(feature)`, `claim_task(id)`,
  `retries_so_far(id)`, and `fail_task(id, reason, *,
  retries_so_far, max_retries)` implementations (`close_task` already
  exists from the transcription branch). "Ready" means open, tagged
  `feature:<name>`, unblocked, and NOT carrying the
  `needs_human_review` label. Retry state is persisted via a
  `turma-retries:<n>` label; budget exhaustion adds
  `needs_human_review` and releases the claim so the exhausted task
  stops appearing as ready. Claim is atomic — bd's state-transition
  semantics ensure no two concurrent runs grab the same task.
- New `WorktreeManager` that creates `./.worktrees/<feature>/<bd-id>/`
  on a dedicated `task/<feature>/<bd-id>` branch, reuses an existing
  worktree if one is found, and cleans up on success.
- New worker-backend protocol with a single pinned v1 implementation:
  Claude Code (non-interactive, `claude -p <prompt>`). Worker prompt
  is assembled from the Beads task's title, description (the verbatim
  subtask list), and repo context; worker writes a `.task_complete`
  sentinel on success or `.task_failed` on failure.
- New `GitAdapter` that owns the commit/push flow between worker
  success and PR creation: dirty-tree detection, `git add -A` +
  `git commit` with a pinned message template, and
  `git push --set-upstream origin <branch>`. Auth relies on the
  operator's existing credentials (ssh agent or `gh auth` git
  helper). A clean tree after a worker's "success" sentinel is
  treated as a worker failure.
- New `PullRequestAdapter` that uses `gh pr create` to open a PR from
  the task branch into the configured base branch with a title/body
  derived from the Beads task fields.
- **Read-only reconciliation on startup:** scan Beads for feature
  tasks in `in_progress` state, cross-check against worktrees + PRs,
  and return a typed `ReconciliationReport`. The module never mutates
  Beads, git, or GitHub; the orchestrator's main loop has an explicit
  repair phase that consumes the report and applies `fail_task`,
  `close_task`, commit/push/PR, or halts on ambiguous findings.
- New `[swarm]` config keys in `turma.example.toml`:
  `worker_backend = "claude-code"`, `worker_timeout = 1800`,
  `max_retries = 1`, `worktree_root = ".worktrees"`,
  `base_branch = "main"`. All have sensible defaults so v1 can run
  without config edits.
- New runtime prerequisite: `gh` CLI on PATH and authenticated with
  `repo` scope (Pull requests: Read and write) for the target
  repository. Absent `gh` raises `PlanningError` with a clear hint.
- `.gitignore` additions for the worktree root and any run-time
  sentinel files (`.task_complete`, `.task_progress`, `.task_failed`).
- `README.md` gets a "Swarm Execution" section covering the
  prerequisites, the one-feature loop, failure modes, and the
  reconciliation-on-resume behavior.

## Capabilities

### New Capabilities

- `swarm-orchestration`: single-feature sequential execution loop over
  a transcribed Beads DAG.
- `worker-backend`: pluggable interface for driving a non-interactive
  coding-agent CLI inside a worktree and detecting completion.
- `worktree-manager`: create/reuse/clean a per-task git worktree at a
  deterministic path on a deterministic branch.
- `git-adapter`: subprocess wrapper for the commit/push flow between
  worker success and PR creation. Mirrors `BeadsAdapter` shape
  (argv pinned by tests, `PlanningError` on non-zero exit).
- `pull-request-adapter`: `gh` CLI wrapper mirroring the
  `BeadsAdapter` shape (subprocess boundary, typed errors, argv
  pinned by unit tests).
- `run-reconciliation`: startup-time read-only inspector that
  compares Beads state to worktrees and PRs and returns a typed
  report. The orchestrator's main loop owns the repair phase that
  consumes the report and mutates state explicitly.

### Modified Capabilities

- `beads-adapter` gains ready/claim/fail methods on top of the
  create/close/list surface shipped with `beads-transcription`.
- `planning-config` gains a `[swarm]` block documented in
  `turma.example.toml`.

## Impact

- New files:
  - `src/turma/swarm/` package entry (`__init__.py`) exposing
    `run_swarm(feature, *, services)` as the public entry.
  - `src/turma/swarm/worktree.py` — worktree creation / reuse /
    cleanup.
  - `src/turma/swarm/worker.py` — `WorkerBackend` protocol plus
    `ClaudeCodeWorker` implementation.
  - `src/turma/swarm/git.py` — `GitAdapter` for commit/push.
  - `src/turma/swarm/pull_request.py` — `PullRequestAdapter`
    subprocess wrapper for `gh pr create`.
  - `src/turma/swarm/reconciliation.py` — read-only startup
    inspector.
  - `tests/test_swarm_*.py` — seven test files, one per module.
- Modified:
  - `src/turma/transcription/beads.py` — extend `BeadsAdapter` with
    the ready/claim/fail methods and the pinned argv shapes for each.
  - `src/turma/cli.py` — replace the `run` placeholder with a real
    `turma run --feature <name>` dispatch. Preserve the existing
    `turma status` placeholder until that feature lands separately.
  - `pyproject.toml` — no new Python deps expected; `gh` is a runtime
    prerequisite, not a package.
  - `.gitignore` — `.worktrees/`, `.task_complete`, `.task_progress`,
    `.task_failed`.
  - `turma.example.toml` — documented `[swarm]` block.
  - `README.md` — new "Swarm Execution" section.
  - `docs/architecture.md` — Execution section updated from the
    current high-level framing to the committed v1 contract.

## Out of Scope

- Parallel task execution. v1 processes one ready task at a time
  before fetching the next batch.
- Multiple worker backends concurrently. Only Claude Code is wired;
  Codex / OpenCode / Gemini CLIs can be added as follow-on
  capabilities once the worker-backend protocol has soaked.
- Provider-pool routing, rate-limit-aware scheduling, or any
  cost-optimization behavior.
- Retry policies beyond a simple per-task retry budget (`max_retries`,
  default 1). Escalation after the budget is a manual recovery step,
  same shape as transcription's orphan-handling.
- Cross-feature concurrency or a multi-feature work queue.
- Review automation, merge automation, or post-merge release flow.
  `turma run` opens the PR and stops.
- Commercial packaging, subscription-tier policies, or anything
  user-facing beyond the one `turma run` command and its config.
