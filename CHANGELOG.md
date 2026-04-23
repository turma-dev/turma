# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project uses Semantic Versioning.

## [Unreleased]

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
