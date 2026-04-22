## Why

The critic loop now produces human-approved `proposal.md` / `design.md` /
`tasks.md` artifacts, but nothing consumes `tasks.md`. Turma's v1 execution
plan routes work through Beads — a git-backed task DAG with atomic claiming
— so `tasks.md` needs a translation layer that produces a Beads epic with
typed tasks, priorities, and dependency edges. Without this layer the
execution orchestrator cannot start and approved plans sit on disk as
prose, unusable by the swarm.

## What Changes

- New `BeadsAdapter` subprocess wrapper for the `bd` CLI, mirroring the
  existing authoring-backend shape: thin class whose `__init__`
  validates `shutil.which("bd")` and whose methods call `subprocess.run`
  and raise typed errors on non-zero exit.
- New pure parser for `tasks.md` that extracts numbered sections
  (`### N. Title`) and their checkbox subtasks. Parser is pure (no
  subprocess, no filesystem), returns a typed representation consumed
  by later phases, and rejects malformed input with a clear reason.
- Translation pipeline that maps the parsed sections onto Beads
  operations: one Beads task per numbered section, priority from
  section order, type inferred from section title keywords with an
  explicit `[type: X]` marker override, and dependency edges
  (`--blocked-by`) inferred from section order by default with explicit
  `[blocked-by: N]` marker override.
- New CLI subcommand `turma plan-to-beads --feature <name>` that gates
  on an `APPROVED` terminal marker in the change directory, invokes the
  parser and translation pipeline, creates the Beads tasks, and writes
  a `TRANSCRIBED.md` marker in the change directory on success.
- Idempotency: re-running refuses unless `--force` is set. `--force`
  requires the prior `TRANSCRIBED.md` to exist and clears before
  re-creating (documented as a deliberate user action).
- `beads` runtime dependency (the `bd` CLI) declared in the README and
  surfaced with a clear error if missing. `bd` is not a Python package
  Turma imports; it is a CLI installed separately (currently via
  `pip install beads` plus Dolt).

## Capabilities

### New Capabilities

- `plan-to-beads`: translate an approved `tasks.md` into a Beads epic
  with typed tasks, priorities, and dependency edges.
- `beads-adapter`: subprocess wrapper for the `bd` CLI following the
  existing authoring-backend pattern.
- `tasks-md-parser`: pure parser of the `tasks.md` shape emitted by the
  critic loop (`### N. Title` sections with `- [ ]` subtasks).

### Modified Capabilities

- `planning-terminal-state`: extended consumers of the `APPROVED`
  terminal marker; `turma plan-to-beads` gates on it authoritatively.

## Impact

- New files:
  - `src/turma/transcription/__init__.py` — translation pipeline entry.
  - `src/turma/transcription/beads.py` — `BeadsAdapter` subprocess
    wrapper.
  - `src/turma/transcription/tasks_md.py` — pure parser.
  - `tests/test_transcription_tasks_md.py`
  - `tests/test_transcription_beads.py`
  - `tests/test_transcription_pipeline.py`
  - `tests/test_transcription_cli.py`
- Modified:
  - `src/turma/cli.py` — `plan-to-beads` subcommand.
  - `README.md` — document the new command surface and the `bd`
    runtime prerequisite.
  - `.gitignore` — Beads / Dolt state if not already covered.
  - `docs/architecture.md` — short Task Translation section update if
    it drifts from the committed contract.

## Out of Scope

- Swarm execution (`turma run`) and worktree orchestration. Covered
  separately; Beads is the consumer of this transcription's output,
  not part of this change.
- Cross-feature dependencies between different Beads epics.
- Automatic re-transcription when a plan is resumed for another round.
  Manual `--force` re-invocation only.
- Beads UI, Dolt tuning, or migration of tasks between epics.
- Task-level acceptance-criteria extraction from subtasks into
  Beads-side structured fields. v1 stores the subtask list verbatim in
  the Beads task body.
