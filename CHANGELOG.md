# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project uses Semantic Versioning.

## [Unreleased]

### Added
- Added `turma run --feature <name> [--max-tasks N] [--backend <id>] [--dry-run]` — a single-feature sequential swarm orchestrator that drives one Beads task at a time from `ready` to `closed` (or `failed` with a retry-budget decision). Each iteration runs `fetch_ready → claim → setup_worktree → run_worker → (sentinel dispatch) → commit → push → open_pr → close_task` against per-task git worktrees under `.worktrees/<feature>/<bd-id>/`, opens one PR per task against a configured base branch, and stops. Review, merge, and release remain human-driven.
- Added the swarm adapter stack under `src/turma/swarm/`: `BeadsAdapter` extensions (`list_ready_tasks`, `list_in_progress_tasks`, `claim_task`, `retries_so_far`, `fail_task`, `get_task_body`), `WorktreeManager` for per-task worktree lifecycle, `WorkerBackend` protocol + `ClaudeCodeWorker` + a named backend registry, `GitAdapter` for the commit/push boundary, and `PullRequestAdapter` for `gh pr create` (including a read-only `find_open_pr_url_for_branch` for reconciliation).
- Added read-only reconciliation (`src/turma/swarm/reconciliation.py`) that walks Beads in-progress tasks, worktree sentinels, and open-PR state to classify prior-run state into six typed findings — `MissingWorktree`, `CompletionPending`, `CompletionPendingWithPr`, `FailurePending`, `StaleNoSentinels`, `OrphanBranch` — consumed by the orchestrator's repair phase before the main loop.
- Added a retry-budget mechanism using Beads labels (`turma-retries:<n>`, `needs_human_review`): `fail_task` records the reason via `bd note`, rotates the retry label, and either releases the claim back to `open` or adds `needs_human_review` on exhaustion. Exhausted-budget failures — whether surfaced by the main loop or by the repair phase — halt the run so the operator can triage via `bd list --label needs_human_review`.
- Added the `[swarm]` configuration block in `turma.example.toml` and config-loader support (`SwarmConfig` in `turma.config`) for `worker_backend`, `worker_timeout`, `max_retries`, `worktree_root`, `base_branch`. `turma run` reads the block via `load_config()` and threads values into `default_swarm_services`; CLI flags (`--backend`, `--max-tasks`) take precedence over config. Missing or partial `[swarm]` blocks fall back to documented defaults.
- Added `docs/smoke-turma-run.md` documenting the end-to-end manual smoke procedure against real `bd` + `gh` + `claude` installs (prerequisites, dry-run, happy path, `completion-pending` reconcile resume, failure → budget-exhaustion path, failure-signature cheat sheet).
- Added 100+ tests under `tests/test_swarm_*.py` covering adapter argv shape, reconciliation finding emission and the read-only invariant, repair-phase dispatch per finding type, main-loop state transitions, budget enforcement, claim-race handling, and CLI wiring.
- Added `turma status --feature <name>` — a read-only readout of a feature's current Beads + GitHub PR + worktree state (counter block, ready / in-progress / pull-requests / orphan-branches sections, with an explicit no-mutation invariant pinned by `tests/test_swarm_status.py`). Reuses the same `[swarm]` config and `default_swarm_services` factory as `turma run`. New module `src/turma/swarm/status.py` with `status_readout(...)` plus two new read-only adapter methods: `BeadsAdapter.list_feature_tasks_all_statuses` (returning `BeadsTaskSnapshot(id, title, labels, status)`) and `PullRequestAdapter.list_prs_for_feature` (returning `PrSummary(number, url, state, title, head_branch)`). The orphan-branches section reuses `reconcile_feature`'s exact `in_progress`-only filter — `turma status` does not redefine the v1 reconciliation contract.

### Changed
- Replaced the placeholder `turma run` CLI with the real dispatch; `--feature` is required, `--max-tasks`, `--backend`, and `--dry-run` are optional. Missing external CLI dependencies (`bd`, `git`, `gh`, `claude`) surface as `PlanningError` at services construction and land in the `error: <msg>` → exit 1 path used by `turma plan` and `turma plan-to-beads`.
- Rewrote `docs/architecture.md`'s Execution section around the committed v1 state machine, authority model (Beads → git → GitHub PR → sentinels), retry-budget label scheme, and the read-only reconciliation contract.
- Documented the Swarm Execution workflow in `README.md` (prerequisites, the one-feature loop, retry-budget and halt conditions, reconciliation-on-resume behavior with the six-category finding table, failure modes, and a worked example).
- No new runtime dependencies in `pyproject.toml`. `gh` (GitHub CLI) and `claude` (Claude Code CLI) are documented external prerequisites. `.worktrees/`, `.task_complete`, `.task_failed`, and `.task_progress` are gitignored.
- Updated `docs/smoke-turma-run.md` for compatibility with `bd 1.0.2` on macOS: documented the `coreutils` / `timeout` prerequisite needed to avoid a `bd init` deadlock against its own pre-commit hook, and replaced the `grep -oE 'bd-smoke-[0-9]+'` id capture with a `bd list … | jq -er '.[0].id'` pipeline so it works with bd 1.0.2's `<prefix>-<hash>` id format and fails loudly on an empty list.


## [0.2.0] - 2026-04-23

### Added
- Added a full author/critic planning loop for `turma plan` with a strict `critique_N.md` format (`## Status: blocking | nits_only | approved`, `[B###]` / `[N###]` / `[Q###]` finding IDs), two-call revision path (per-finding `response_N.md` + revised artifacts), LangGraph state machine with SQLite checkpointing, and `max_rounds` + repeated-blocking-ID loop detection.
- Added a resume CLI: `turma plan --feature <name> --resume` for read-only status, plus `--approve`, `--revise "<why>"`, `--abandon "<why>"`, and `--approve --override "<why>"` for advancing or halting from `awaiting_human_approval` / `needs_human_review`.
- Added terminal-marker reconciliation so `APPROVED`, `ABANDONED.md`, `NEEDS_HUMAN_REVIEW.md`, and `OVERRIDE.md` are authoritative state; re-running `turma plan` on an already-terminal change is a read-only no-op.
- Added `turma plan-to-beads --feature <name> [--force]` that translates an approved plan's `tasks.md` into a feature-tagged Beads task set with parser-to-bd type translation (`impl`/`test` → `task`, `docs` → `chore`, `spec` → `decision`), bd-native priority mapping, dependency edges, and a `TRANSCRIBED.md` marker. `--force` handles both marker-recorded teardown and orphan-only teardown.
- Added the `BeadsAdapter` subprocess wrapper, the `tasks.md` parser, and the transcription pipeline under `src/turma/transcription/` with 89 new tests (parser, adapter argv pinning, pipeline routing, CLI dispatch, error mapping).
- Added `docs/smoke-plan-to-beads.md` documenting the end-to-end manual smoke procedure against a real `bd` database (happy path, both `--force` teardown paths, malformed-marker rejection, failure-signature cheat sheet).

### Changed
- Replaced the single-pass `turma plan` flow with the graph-driven critic loop; config keys `critic_model`, `max_rounds`, and `interactive` are now behaviorally wired and documented in `turma.example.toml`.
- Suspension output now prints the exact resume commands (`turma plan --feature <name> --resume --approve | --revise | --abandon | --approve --override`) and the correct round's critique path, with an `interactive = false` confirmation path that halts without auto-approving.
- Documented `bd` (Beads) and Dolt as external runtime prerequisites for `turma plan-to-beads` in `README.md` with a "Plan-to-Beads" section covering prerequisites, behavior, `--force` semantics, and partial-failure recovery.
- Rewrote `docs/architecture.md`'s Planning section around the committed v1 state machine and the Task Translation section around the section-level Beads model, parser-to-bd type mapping, and label-based (not native-epic) feature association.
- New runtime dependencies: `langgraph>=1.1.9` and `langgraph-checkpoint-sqlite>=3.0.3` in `pyproject.toml`. `.langgraph/` and `openspec/changes/*/PLANNING_STATE.json` added to `.gitignore`.

## [0.1.7] - 2026-04-08

### Added
- Added Gemini as a supported `turma plan` author backend via the `gemini` CLI.
- Added backend and planning test coverage for Gemini-backed artifact generation.

### Changed
- Updated public docs and config examples to reflect Gemini support in `turma plan`.
- Documented the Gemini headless trust invariant behind the read-only planning safety boundary.

## [0.1.6] - 2026-04-06

### Added
- Added OpenCode as a supported `turma plan` author backend via provider/model routing.

### Changed
- Documented that `turma plan` planning quality depends on the selected provider/model even though Claude, Codex, and OpenCode transport paths are supported.

## [0.1.5] - 2026-04-06

### Added
- Added `turma plan` as a working single-pass author workflow for generating OpenSpec `proposal`, `design`, and `tasks` artifacts.
- Added provider-specific planning backends for both Claude and Codex.
- Added config loading and planning validation tests.

### Changed
- Tightened planning artifact validation to reject empty output, clarification requests, and malformed template structure.
- Updated public docs to reflect the current `turma plan` capability and remaining planning limitations.

## [0.1.4] - 2026-04-05

### Added
- Added OpenSpec workflow scaffolding for Claude-based repo-local commands and skills.
- Added the first real feature spec under `openspec/changes/turma-init/`.
- Added automated tests for `turma init`.
- Added a minimal GitHub Actions CI workflow for install and test validation.

### Changed
- Implemented `turma init` as the first non-stub CLI command.
- Updated public docs to reflect the OpenSpec workflow and current command status.

## [0.1.3] - 2026-04-04

### Changed
- Established the first clean post-rewrite public release line for the Turma GitHub repository.
- Removed private planning and commercial roadmap material from the public repo history.
- Realigned the public GitHub history with future package releases after the `0.1.2` PyPI publication.

## [0.1.2] - 2026-04-03

### Added
- Adopted a `uv`-based development workflow with a tracked `uv.lock`.
- Committed `turma.example.toml` as the tracked configuration template.
- Added a repo-wide `AGENTS.md` as the canonical contributor and agent guide.

### Changed
- Ignored local `turma.toml` so personal provider and concurrency settings remain untracked.
- Strengthened reconciliation and local-state guidance in the architecture docs.
- Validated the package entry points with `uv run turma --help`, `uv run python -m turma --help`, and `uv run pytest`.

## [0.1.0] - 2026-04-03

### Added
- Initial Turma Python project scaffold.
- Minimal `turma` CLI entry point with `init`, `plan`, `run`, and `status` commands.
- Project `.gitignore` covering macOS, Vim, Python, logs, and local orchestration state.
- `.claude/commands/plan-to-epic.md` command scaffold.
- `.agents/` role guidance for planning and implementation agents.
- Reorganized long-form docs under `docs/`.
- Initial README draft and architecture documentation.
