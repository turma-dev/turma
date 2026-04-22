## Tasks

### 1. Extract planner primitives, inject backend interfaces

- [ ] Split `src/turma/planning.py` into a `src/turma/planning/`
      package while preserving the public entry point.
- [ ] Factor the current single-pass flow into reusable operations:
      load config + role prompts, scaffold the change directory,
      build prompts, select backend by model, generate artifacts,
      write artifacts.
- [ ] Convert `_get_backend()` into an injectable interface
      (function or class) so tests can substitute fakes without
      invoking real CLIs.
- [ ] No behavior change: `turma plan <feature>` on a clean repo
      still produces `proposal.md`, `design.md`, `tasks.md` in the
      current order.
- [ ] Existing planning tests continue to pass unchanged.

### 2. Add critique parser

- [ ] New module `src/turma/planning/critique_parser.py`. Pure;
      no LangGraph, no backend calls.
- [ ] Parse `## Status: blocking | nits_only | approved` (exact
      tokens). Any other value or missing line → route decision
      `needs_human_review`.
- [ ] Parse finding IDs matching `[B###]`, `[N###]`, `[Q###]`.
      Missing or malformed IDs → `needs_human_review`.
- [ ] Return a typed route result consumed by later phases.
- [ ] Full unit coverage in `tests/test_critique_parser.py`:
      all three Status tokens, ID format failures, missing Status,
      mixed-severity finding lines, questions under blocking vs
      nits_only.

### 3. Minimal author → critic round runner

- [ ] After initial author generation, invoke the critic using
      `critic_model` from config and the role prompt in
      `.agents/critic.md`.
- [ ] Write `critique_1.md` into the change directory.
- [ ] Parse via phase 2 and return the route decision. No loop,
      no persistence, no human gate yet.
- [ ] `tests/test_planning_round_runner.py` drives the round
      runner with injected author/critic fakes covering each
      status → route mapping.

### 4. State machine and checkpointing

- [ ] Add dependency on `langgraph` (and a SQLite checkpointer)
      to `pyproject.toml`.
- [ ] Add `.langgraph/` and
      `openspec/changes/*/PLANNING_STATE.json` to `.gitignore`.
- [ ] New module `src/turma/planning/state_machine.py`.
- [ ] Implement states: `drafting`, `critic_review`,
      `needs_revision`, `awaiting_human_approval`,
      `needs_human_review`, terminal (`approved`, `abandoned`).
- [ ] `interrupt_before` suspends at `awaiting_human_approval`.
- [ ] Checkpoint path convention: `./.langgraph/<feature>.db`.
- [ ] Write `PLANNING_STATE.json` to the change directory on
      every transition (recovery hint, not source of truth).
- [ ] `tests/test_planning_state_machine.py`: using injected
      author/critic fakes, drive a feature from round 1 through
      `critic_review` into `awaiting_human_approval` with the graph
      suspended and both the checkpoint and `PLANNING_STATE.json`
      persisted.

### 5. Resume CLI

- [ ] New module `src/turma/planning/resume.py` (or equivalent).
- [ ] Extend `src/turma/cli.py` with:
  - [ ] `turma plan --resume <feature>` — read-only status.
  - [ ] `--approve` — writes `APPROVED`.
  - [ ] `--revise "<why>"` — writes `response_N_human.md`,
        advances to `needs_revision`.
  - [ ] `--abandon "<why>"` — writes `ABANDONED.md`, terminal.
  - [ ] `--approve --override "<why>"` — writes `OVERRIDE.md`
        then `APPROVED`; allowed only from halted
        `needs_human_review`.
- [ ] Each flag becomes a structured resume payload injected into
      the suspended approval node. Filesystem markers are side
      effects, not triggers.
- [ ] `tests/test_planning_resume.py`: full end-to-end validation
      of `--approve`, `--abandon`, and `--approve --override`.
      `--revise` is tested only for producing the correct suspended
      state — the downstream revision path is intentionally
      incomplete until task 6; call this out in the test docstring
      so it is not mistaken for a bug.

### 6. Two-call revision path

- [ ] Add a "response" mode to the author backend invocation that
      produces `response_N.md` given `critique_N.md` + prior
      artifacts.
- [ ] Add a "revision" mode that produces revised
      `proposal.md` / `design.md` / `tasks.md` given
      `response_N.md`.
- [ ] Enforce the partial-failure rule: if response generation
      succeeds and revised-draft generation fails,
      `response_N.md` remains UNCOMMITTED; retry resumes from the
      filesystem artifact; commit only after both files exist.
- [ ] Advance the round counter and loop back into `critic_review`.
- [ ] Extend `tests/test_planning_state_machine.py` to cover a
      round-1 `blocking` → round-2 `critic_review` transition with
      the two-call contract.

### 7. Loop budget, recovery, end-to-end mocked tests

- [ ] Enforce `max_rounds` (default 4). Exhaustion →
      `needs_human_review`; never auto-approve.
- [ ] Implement loop detection: two consecutive rounds with
      identical unresolved blocking finding ID sets →
      `needs_human_review`.
- [ ] Implement recovery/reconciliation in the graph entry path
      using the documented authority order: terminal artifacts →
      latest critique/response files → checkpoint → git → JSON
      hint.
- [ ] End-to-end mocked test suite covering each terminal state
      (`approved`, `abandoned`, `needs_human_review`, override)
      and every authority-ordering rule.
- [ ] Confirm `interactive = false` halts at
      `awaiting_human_approval`, prints the resume commands, and
      exits cleanly without auto-approving.

### 8. Docs and config surface

- [ ] Update `turma.example.toml`: document observable behavior
      of `critic_model`, `max_rounds`, `interactive` (replace any
      "currently unused" wording).
- [ ] Update `README.md`: describe the planning loop and the
      `--resume` command surface.
- [ ] Confirm `docs/architecture.md` matches the committed v1
      contract; update only the author/critic loop section if it
      drifts.
