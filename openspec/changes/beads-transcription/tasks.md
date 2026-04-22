## Tasks

### 1. Add the tasks.md parser

- [ ] New module `src/turma/transcription/tasks_md.py`. Pure; no
      subprocess, no filesystem.
- [ ] Parse `## Tasks` + `### <N>\. <title>` section headings + `- [ ]`
      subtask bullets into a typed ordered representation.
- [ ] Support optional `[type: impl | test | docs | spec]` inline
      section marker. Unknown token → parse failure.
- [ ] Support optional `[blocked-by: N]` / `[blocked-by: N, M]` inline
      section marker. Reject forward reference, self reference, and
      references to non-existent section numbers.
- [ ] Default dependency: each section depends on the previous; section
      1 has no edges.
- [ ] Default type inference from title keywords (test/tests → test;
      doc/docs/readme → docs; spec/specification → spec; else impl).
- [ ] Reject: missing `## Tasks`, non-ascending section numbers, empty
      sections, malformed markers.
- [ ] Return a typed ParseResult union: `ParsedTasks(...)` or
      `TasksParseFailure(reason, ...)`.
- [ ] Full unit coverage in `tests/test_transcription_tasks_md.py`:
      happy path, default type inference (all four kinds), explicit
      type markers, default sequential dependencies, explicit
      `blocked-by` markers, every parse-failure category.

### 2. Add the Beads subprocess adapter

- [ ] New module `src/turma/transcription/beads.py`.
- [ ] `BeadsAdapter.__init__` validates `shutil.which("bd")` and raises
      `PlanningError` with a `pip install beads` hint on failure.
- [ ] `create_task(title, body, task_type, priority, blocked_by_ids)`
      returns the new `bd` task id. Builds argv as documented
      (`bd create --type ... --priority ... [--blocked-by ...] title`).
      Parses the id from `bd` stdout (JSON preferred if available,
      otherwise text).
- [ ] `close_task(task_id)` runs `bd close <id>`, raises on non-zero.
- [ ] `list_epic(feature)` runs `bd ls --json` and filters on the
      `feature: <name>` first-line tag Turma writes into each task
      body.
- [ ] All non-zero exits raise `PlanningError` with `bd` stderr
      preserved verbatim.
- [ ] Unit tests in `tests/test_transcription_beads.py` using
      subprocess stubs covering create, close, list, missing-CLI, and
      non-zero-exit paths.

### 3. Wire the translation pipeline

- [ ] `src/turma/transcription/__init__.py` exposes
      `transcribe_to_beads(feature, services, *, force=False)` that
      returns the created task IDs on success.
- [ ] Gate on `reconcile_current_state(session) == "approved"` before
      any other work.
- [ ] Gate on `TRANSCRIBED.md`: reject without `--force`; require it
      with `--force`.
- [ ] Implement the pipeline: parse, iterate sections in order,
      resolve `blocked_by_ids` from prior create calls, invoke the
      adapter.
- [ ] On full success, write `TRANSCRIBED.md` with feature name,
      timestamp, and the created task IDs in section order.
- [ ] On `--force` with prior `TRANSCRIBED.md`: invoke teardown
      (close recorded IDs in reverse order, delete `TRANSCRIBED.md`),
      then run the pipeline.
- [ ] Partial-failure behavior matches the design: no automated
      rollback, clear error surfacing `bd` stderr.
- [ ] Integration tests in `tests/test_transcription_pipeline.py`
      using a stub `BeadsAdapter` and an approved fixture change dir.

### 4. Wire the CLI subcommand

- [ ] Add `plan-to-beads` subparser to `src/turma/cli.py` with
      `--feature` (required) and `--force` (flag).
- [ ] Dispatch to `transcribe_to_beads(feature, services,
      force=args.force)`. Reuse the existing
      `default_planning_services()` factory where the adapter can be
      substituted for tests.
- [ ] Map `PlanningError` to a non-zero exit and print the message.
- [ ] `tests/test_transcription_cli.py`: invocations, exit codes,
      stray-flag rejection, `--force` combinations.

### 5. Docs and config surface

- [ ] Update `README.md`: new "Plan-to-Beads" section documenting
      the command, the `bd` runtime prerequisite, the `--force`
      semantics, and the partial-failure manual recovery.
- [ ] Update `docs/architecture.md` "Task Translation" section if it
      drifts from the committed v1 contract here.
- [ ] `.gitignore`: ensure any local `bd` / Dolt state outside the
      repo root is covered if needed (usually not — `.beads/` is the
      repo-local state and is already conventional).
- [ ] Note in `turma.example.toml` if any new config fields are
      introduced. v1 does NOT add any; transcription has no tunable
      knobs other than `--force`.

### 6. End-to-end validation

- [ ] Smoke script or documented manual run on a small real feature:
      `turma plan --feature demo` → iterate to approved →
      `turma plan-to-beads --feature demo` → `bd ls` shows the
      expected typed tasks in the expected dependency order.
- [ ] Verify `--force` teardown path end-to-end on the same fixture.
- [ ] Record the result in a CHANGELOG entry or PR body; no new
      automated harness beyond the unit + pipeline tests.
