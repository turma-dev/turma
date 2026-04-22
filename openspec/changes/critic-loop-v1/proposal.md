## Why

`turma plan` is currently single-pass: the author backend generates
`proposal.md`, `design.md`, and `tasks.md` once and exits. There is no
adversarial review, no human approval gate, and no revision loop —
even though `turma.example.toml` already reserves `critic_model`,
`max_rounds`, and `interactive` fields. The intended planning workflow
is an author/critic iteration that converges on a spec precise enough
to implement, followed by a load-bearing human approval gate before
any implementation work begins. This change wires that workflow up.

## What Changes

- New planning state machine (`drafting → critic_review → {needs_revision | awaiting_human_approval} → terminal`)
  built on LangGraph with SQLite checkpointing.
- Critic invocation added after each author round, using the
  `critic_model` config value.
- Strict machine-readable critique format (`## Status: …`, finding IDs
  like `[B001]`) with a dedicated parser module.
- Two-call author revision contract: a `response_N.md` artifact first,
  then revised `proposal.md` / `design.md` / `tasks.md`.
- Resume CLI:
  - `turma plan --resume <feature>` — read-only status
  - `--approve` — writes `APPROVED` from `awaiting_human_approval`
  - `--revise "<why>"` — writes `response_N_human.md`, advances a round
  - `--abandon "<why>"` — writes `ABANDONED.md`
  - `--approve --override "<why>"` — writes `OVERRIDE.md` then
    `APPROVED`, allowed only from halted `needs_human_review`
- Terminal artifacts defined for every end state: `APPROVED`,
  `ABANDONED.md`, `NEEDS_HUMAN_REVIEW.md`, plus `OVERRIDE.md` as
  recovery evidence.
- Recovery rules: filesystem terminal markers are authoritative for
  current state; LangGraph checkpoint and git history are hints.
- New dependencies: `langgraph` and a SQLite checkpointer added to
  `pyproject.toml`. `.langgraph/` and generated planning state hints
  are added to `.gitignore`.

## Capabilities

### New Capabilities

- `planning-critic-loop`: iterative author/critic refinement with
  strict critique parsing and stable finding IDs for loop detection.
- `planning-human-gate`: explicit human approval gate with structured
  resume commands; critic approval never ends the loop on its own.
- `planning-recovery`: resumable planning from any suspended state
  using filesystem terminal markers as authority.

### Modified Capabilities

- `planning-state-machine`: `turma plan` becomes a graph-driven loop,
  not a linear pass. `interactive = false` halts at the human gate and
  prints resume commands rather than auto-approving.
- `planning-artifacts`: `openspec/changes/<feature>/` gains
  `critique_N.md`, `response_N.md`, `response_N_human.md`,
  `PLANNING_STATE.json`, `APPROVED`, `ABANDONED.md`,
  `NEEDS_HUMAN_REVIEW.md`, and `OVERRIDE.md` alongside the existing
  three author artifacts.
- `planning-config`: `critic_model`, `max_rounds`, and `interactive`
  become behaviorally wired (they are currently loaded but unused).

## Impact

- New files:
  - `src/turma/planning/` package (split from current `planning.py`),
    including `critique_parser.py`, `state_machine.py`, `resume.py`,
    `markers.py`.
  - `tests/test_critique_parser.py`, `tests/test_planning_state_machine.py`,
    `tests/test_planning_resume.py`.
- Modified:
  - `src/turma/planning.py` → refactored into the new package; public
    entry point preserved.
  - `src/turma/cli.py` → new `--resume` flag group.
  - `pyproject.toml` → `langgraph` + SQLite checkpointer dependency.
  - `.gitignore` → `.langgraph/` and generated planning state hints.
  - `turma.example.toml` → documents observable behavior of
    `interactive`, `max_rounds`, `critic_model`.
  - `README.md` → documents the planning loop and resume commands.

## Out of Scope

- Beads transcription (`/plan-to-epic`). Tracked separately.
- Worktree orchestration and the implementation swarm.
- The `specs/` artifact (deferred until OpenSpec `specs/` generation is
  added).
- Per-round human override of blocking critiques (v1 only offers
  override from terminal `needs_human_review`).
