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
      `PlanningError` with a `brew install beads` hint on failure
      (Beads ships as a Go binary via Homebrew; it is not a PyPI
      package).
- [ ] Body-writing mechanism chosen per `bd create --help` (Beads
      1.0.2): inline `-d` / `--description <text>` flag. No fallback
      mechanism needed. Chosen mechanism is recorded in the
      `BeadsAdapter` class docstring.
- [ ] `create_task(*, title, description, bd_type, priority, feature,
      extra_labels=(), blocker_ids=())` returns the new `bd` task id
      as a `str`. Feature association is recorded via the
      `feature:<name>` label (first entry of `--labels`, followed by
      any `extra_labels` comma-joined). Description is passed via
      `--description`. Adapter receives bd-native types
      (`bug|feature|task|epic|chore|decision`) and bd-native priority
      in `[0,4]`; parser-type → bd-type translation lives in the
      pipeline (Task 3).
- [ ] For each `blocker_id`, follow the create with
      `bd dep add <new-id> <blocker-id>` (direction: new depends on
      blocker). `bd create --deps` is not used — its `blocks:<id>`
      form is inverted from what Turma needs.
- [ ] Parse the returned `bd` task id from stdout (`--silent` makes
      create emit only the id).
- [ ] `close_task(task_id)` runs `bd close <id>`, raises on non-zero.
- [ ] `list_feature_tasks(feature)` runs
      `bd list --label feature:<feature> --json --limit 0` and returns
      a tuple of `BeadsTaskRef(id, title, labels)` records for open
      tasks.
- [ ] All non-zero exits raise `PlanningError` with `bd` stderr
      preserved verbatim (falling back to stdout if stderr is empty).
- [ ] Unit tests in `tests/test_transcription_beads.py` using
      subprocess stubs covering create (argv shape pinned, extra
      labels, blocker follow-up calls), close, list (argv pinned,
      empty, non-JSON, non-array, missing-id rows), missing-CLI, and
      non-zero-exit paths including the orphan-on-dep-failure case.
      The `VALID_BD_TYPES` frozenset is pinned to the upstream set as
      a canary for bd CLI drift.

### 3. Wire the translation pipeline

- [ ] `src/turma/transcription/__init__.py` exposes
      `transcribe_to_beads(feature, adapter, *, force=False)` that
      returns a `TranscriptionResult(feature, ids_by_section,
      transcribed_path)`. The adapter is injected directly (not via
      `PlanningServices`) because transcription has none of planning's
      backend / role / openspec-CLI needs; passing a heavyweight
      session is disproportionate.
- [ ] Gate on the `APPROVED` terminal marker in the change directory
      before any other work. Checking the file directly is equivalent
      to `reconcile_current_state(session) == "approved"` for the top
      tier of the authority order, without the session dependency.
- [ ] Preflight `TRANSCRIBED.md`: refuse without `--force` if present.
- [ ] Preflight `list_feature_tasks(feature)` when `TRANSCRIBED.md` is
      absent: if the adapter returns any orphans, refuse without
      `--force` and surface the orphan IDs in the error message with
      both manual and `--force` recovery paths.
- [ ] Implement the pipeline: parse, iterate sections in order,
      translate parser task_type → bd_type (`impl`/`test` → `task`,
      `docs` → `chore`, `spec` → `decision`) and parser priority →
      bd priority (`min(N - 1, 4)`), resolve `blocker_ids` from prior
      create calls, and invoke
      `BeadsAdapter.create_task(title=..., description=...,
      bd_type=..., priority=..., feature=..., extra_labels=(
      f"turma-type:{parser_type}",), blocker_ids=...)`. Record each
      returned id keyed by section number for dependency resolution
      of later sections.
- [ ] On full success, write `TRANSCRIBED.md` with feature name,
      timestamp, and the created task IDs in section order.
- [ ] `--force` teardown: if `TRANSCRIBED.md` is present, close the
      recorded IDs in reverse order and delete the marker. Else if
      `list_feature_tasks(feature)` returns orphans, close each orphan
      id. Else no-op. Then run the pipeline from scratch.
- [ ] Partial-failure behavior matches the design: no automated
      rollback, clear error surfacing `bd` stderr and orphan IDs.
- [ ] Integration tests in `tests/test_transcription_pipeline.py`
      using a stub `BeadsAdapter` and an approved fixture change dir.

### 4. Wire the CLI subcommand

- [ ] Add `plan-to-beads` subparser to `src/turma/cli.py` with
      `--feature` (required) and `--force` (flag).
- [ ] Construct a `BeadsAdapter()` and dispatch to
      `transcribe_to_beads(feature, adapter, force=args.force)`. The
      adapter's `__init__` handles the `bd` missing-CLI check and
      surfaces the `brew install beads` hint. Tests can inject a stub
      adapter by calling `transcribe_to_beads` directly.
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
