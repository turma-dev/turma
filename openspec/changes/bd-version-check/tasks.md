# Tasks: bd version check

Test-first throughout: each task writes the failing tests for its
acceptance criteria before the implementation.

## Task 1: version parser and comparator

- [ ] 1.1 Failing tests for `parse_bd_version`: `1.0.2`, `v1.0.2`,
      `1.0.2+homebrew.1`, `1.3.1-rc.1`, and `1.3.1-rc.1+abc123` parse;
      `dev`, `unknown`, `""`, `1.0`, and `1.0.2.3` return `None`.
- [ ] 1.2 Failing parameterized test for classification: every row of
      the design's boundary table (`1.0.1` through `1.3.0`), covering
      pre-release below its release at the minimum, pre-release by
      release base at the ceiling, and build metadata ignored.
- [ ] 1.3 Implement `BdVersion`, `parse_bd_version`, the classifier,
      and the two constants in `src/turma/transcription/beads.py`.
      These are pure functions: no subprocess.

**Done when:** the parser and classification tests pass. No behaviour
change to `BeadsAdapter` yet.

## Task 2: the check in `BeadsAdapter.__init__`

- [ ] 2.1 Update the shared adapter test fixtures so every existing
      construction receives an in-range `bd version --json` response.
      The existing suite stays green before any new behaviour is added.
- [ ] 2.2 Failing tests, one per row of the design's outcomes table:
      in range proceeds silently; below the minimum refuses even with
      the override; at or above the ceiling refuses by default; the
      ceiling with `TURMA_ALLOW_UNTESTED_BD=1` warns on stderr and
      proceeds; `TURMA_ALLOW_UNTESTED_BD=true` doesn't override;
      undetermined warns and proceeds.
- [ ] 2.3 Failing tests: a non-zero exit, a timeout, and invalid JSON
      each count as undetermined; stderr from a successful
      `bd version` is ignored.
- [ ] 2.4 Failing tests: the refusal and warning text render the range
      from the constants (change the constants in the test, and the
      message follows).
- [ ] 2.5 Failing test: no other `bd` subprocess runs before the check,
      i.e. `bd version --json` is the first and only bd call made
      during construction.
- [ ] 2.6 Implement the check directly after the existing
      `shutil.which` check, with a 5s timeout.

**Done when:** all Task 2 tests pass and the full `uv run pytest` suite
is green.

## Task 3: command-level behaviour and docs

- [ ] 3.1 Failing CLI tests: `plan-to-beads`, `run`, and `status`
      each exit 1 with the refusal message when bd is out of range, and
      touch no Beads state (no mutation calls recorded).
- [ ] 3.2 Failing tests: `run --json` and `status --json` report a
      refusal through their existing error path (the `turma.run.v1`
      `error` event and the `turma.status.v1` error object), and an
      undetermined-version warning doesn't appear on stdout.
- [ ] 3.3 Real-tool test, skipped when `bd` isn't on PATH and run with
      `TURMA_ALLOW_UNTESTED_BD` unset: call `bd version --json`
      directly and assert that the output parses and its `version`
      field is accepted by `parse_bd_version`. Then construct
      `BeadsAdapter()` and assert that the outcome matches that
      version's classification: success when in range, `PlanningError`
      when below minimum or untested. The test passes on any installed
      bd, not just 1.0.2.
- [ ] 3.4 Docs: `README.md` (the Beads install section and the
      prerequisites list gain the supported range and
      `TURMA_ALLOW_UNTESTED_BD`), and a short note where
      `docs/architecture.md` describes the Beads adapter.
- [ ] 3.5 Update the install-hint and README links from
      `steveyegge/beads` to `gastownhall/beads` (design Decision 3).

**Done when:** the CLI tests pass, the real-tool test passes locally,
the docs state the range, `openspec validate bd-version-check --strict`
passes, and the validation baseline in `AGENTS.md` passes.

## Follow-up (separate change, not in this one)

- Raise `BD_MAX_EXCLUSIVE` to cover 1.3.x after a live pooled smoke
  run on bd 1.3.0. Confirm that the merge-cleanliness revert logic and
  the bd-state-clean preflight behave correctly with 1.0.5's opt-in
  export defaults, and that 1.3.0's schema migration on first open
  doesn't dirty the tree.
