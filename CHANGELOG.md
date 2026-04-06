# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project uses Semantic Versioning.

## [Unreleased]

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
