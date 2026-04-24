## Tasks

### 1. Extract planner primitives, inject backend interfaces

- [x] Split `src/turma/planning.py` into a `src/turma/planning/`
      package while preserving the public entry point.
- [x] Factor the current single-pass flow into reusable operations:
      load config + role prompts, scaffold the change directory,
      build prompts, select backend by model, generate artifacts,
      write artifacts.
- [x] Convert `_get_backend()` into an injectable interface
      (function or class) so tests can substitute fakes without
      invoking real CLIs.
- [x] No behavior change: `turma plan <feature>` on a clean repo
      still produces `proposal.md`, `design.md`, `tasks.md` in the
      current order.
- [x] Existing planning tests continue to pass unchanged.

### 2. Add critique parser

- [x] New module `src/turma/planning/critique_parser.py`. Pure;
      no LangGraph, no backend calls.
- [x] Parse `## Status: blocking | nits_only | approved` (exact
      tokens). Any other value or missing line → route decision
      `needs_human_review`.
- [x] Parse finding IDs matching `[B###]`, `[N###]`, `[Q###]`.
      Missing or malformed IDs → `needs_human_review`.
- [x] Return a typed route result consumed by later phases.
- [x] Full unit coverage in `tests/test_critique_parser.py`:
      all three Status tokens, ID format failures, missing Status,
      mixed-severity finding lines, questions under blocking vs
      nits_only.

### 3. Minimal author → critic round runner

- [x] After initial author generation, invoke the critic using
      `critic_model` from config and the role prompt in
      `.agents/critic.md`.
- [x] Write `critique_1.md` into the change directory.
- [x] Parse via phase 2 and return the route decision. No loop,
      no persistence, no human gate yet.
- [x] `tests/test_planning_round_runner.py` drives the round
      runner with injected author/critic fakes covering each
      status → route mapping.

### 4. State machine and checkpointing

- [x] Add dependency on `langgraph` (and a SQLite checkpointer)
      to `pyproject.toml`.
- [x] Add `.langgraph/` and
      `openspec/changes/*/PLANNING_STATE.json` to `.gitignore`.
- [x] New module `src/turma/planning/state_machine.py`.
- [x] Implement states: `drafting`, `critic_review`,
      `needs_revision`, `awaiting_human_approval`,
      `needs_human_review`, terminal (`approved`, `abandoned`).
- [x] `interrupt_before` suspends at `awaiting_human_approval`.
- [x] Checkpoint path convention: `./.langgraph/<feature>.db`.
- [x] Write `PLANNING_STATE.json` to the change directory on
      every transition (recovery hint, not source of truth).
- [x] `tests/test_planning_state_machine.py`: using injected
      author/critic fakes, drive a feature from round 1 through
      `critic_review` into `awaiting_human_approval` with the graph
      suspended and both the checkpoint and `PLANNING_STATE.json`
      persisted.

### 5. Resume CLI

- [x] New module `src/turma/planning/resume.py` (or equivalent).
- [x] Extend `src/turma/cli.py` with:
  - [x] `turma plan --resume <feature>` — read-only status.
  - [x] `--approve` — writes `APPROVED`.
  - [x] `--revise "<why>"` — writes `response_N_human.md`,
        advances to `needs_revision`.
  - [x] `--abandon "<why>"` — writes `ABANDONED.md`, terminal.
  - [x] `--approve --override "<why>"` — writes `OVERRIDE.md`
        then `APPROVED`; allowed only from halted
        `needs_human_review`.
- [x] Each flag becomes a structured resume payload injected into
      the suspended approval node. Filesystem markers are side
      effects, not triggers.
- [x] `tests/test_planning_resume.py`: full end-to-end validation
      of `--approve`, `--abandon`, and `--approve --override`.
      `--revise` is tested only for producing the correct suspended
      state — the downstream revision path is intentionally
      incomplete until task 6; call this out in the test docstring
      so it is not mistaken for a bug.

### 6. Two-call revision path

- [x] Add a "response" mode to the author backend invocation that
      produces `response_N.md` given `critique_N.md` + prior
      artifacts.
- [x] Add a "revision" mode that produces revised
      `proposal.md` / `design.md` / `tasks.md` given
      `response_N.md`.
- [x] Enforce the partial-failure rule: if response generation
      succeeds and revised-draft generation fails,
      `response_N.md` remains UNCOMMITTED; retry resumes from the
      filesystem artifact; commit only after both files exist.
- [x] Advance the round counter and loop back into `critic_review`.
- [x] Extend `tests/test_planning_state_machine.py` to cover a
      round-1 `blocking` → round-2 `critic_review` transition with
      the two-call contract.

### 7. Loop budget, recovery, end-to-end mocked tests

- [x] Enforce `max_rounds` (default 4). Exhaustion →
      `needs_human_review`; never auto-approve.
- [x] Implement loop detection: two consecutive rounds with
      identical unresolved blocking finding ID sets →
      `needs_human_review`.
- [x] Implement recovery/reconciliation in the graph entry path
      using the documented authority order: terminal artifacts →
      latest critique/response files → checkpoint → git → JSON
      hint.
- [x] End-to-end mocked test suite covering each terminal state
      (`approved`, `abandoned`, `needs_human_review`, override)
      and every authority-ordering rule.
- [x] Confirm `interactive = false` halts at
      `awaiting_human_approval`, prints the resume commands, and
      exits cleanly without auto-approving.

### 8. Docs and config surface

- [x] Update `turma.example.toml`: document observable behavior
      of `critic_model`, `max_rounds`, `interactive` (replace any
      "currently unused" wording).
- [x] Update `README.md`: describe the planning loop and the
      `--resume` command surface.
- [x] Confirm `docs/architecture.md` matches the committed v1
      contract; update only the author/critic loop section if it
      drifts.
