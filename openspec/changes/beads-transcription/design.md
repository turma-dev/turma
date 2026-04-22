## Scope

Covers translation of an approved `tasks.md` into a feature-tagged task
set in Beads for a single Turma feature. Ends when the Beads tasks
exist and a `TRANSCRIBED.md` marker is written in the change directory.

**Terminology note.** Beads has a native "epic" concept (hierarchical
IDs like `bd-a3f8` / `bd-a3f8.1`), but its creation and
task-to-epic association APIs are not documented in the current
upstream README. v1 deliberately does NOT use native Beads epics;
instead it records a feature association via the task body / description
(or, as a fallback, a title prefix — see `BeadsAdapter` below).
Migration to native epics is a deferred open item once the relevant
`bd` commands are validated.

Out of scope:

- Swarm execution (`turma run`).
- Authoring or revising `tasks.md` (already handled by the critic loop).
- Beads itself — this change consumes `bd`, it does not modify it.
- Dolt installation, Beads database migration, cross-epic dependencies.

## Authority model

The `APPROVED` terminal marker is the sole gate. Transcription MUST NOT
run against a change that is not approved. The existing
`reconcile_current_state(session)` helper returns the terminal state; the
transcription command short-circuits unless that helper returns
`"approved"`.

Idempotency is two-layer:

1. `TRANSCRIBED.md` marker: written after a fully successful
   transcription. Its presence is the successful-run signal.
2. `BeadsAdapter.list_feature_tasks(feature)`: when `TRANSCRIBED.md` is
   absent, this is the orphan detector. Non-empty result means a prior
   attempt crashed mid-pipeline and left feature-tagged tasks in Beads
   without writing the marker. The command refuses and surfaces the
   orphan IDs so the operator can close them manually (or re-invoke
   with `--force`; see below).

`--force` handles both recovery paths:

- If `TRANSCRIBED.md` exists, use its recorded IDs for teardown.
- If `TRANSCRIBED.md` is absent but orphan feature-tagged tasks exist,
  fall back to `list_feature_tasks(feature)` for teardown.
- If neither is present, `--force` is a no-op on teardown and the
  pipeline runs from scratch.

## Command surface

```
turma plan-to-beads --feature <name>
turma plan-to-beads --feature <name> --force
```

Behavior:

- Without `--force`:
  - Fails fast if `TRANSCRIBED.md` already exists.
  - Fails fast if `list_feature_tasks(feature)` returns any tasks
    (orphans from a prior failed transcription). The error prints
    the orphan IDs and the manual recovery command.
- With `--force`:
  - If `TRANSCRIBED.md` exists, close the IDs it recorded and delete
    the marker, then re-run the pipeline.
  - Else if `list_feature_tasks(feature)` returns orphans, close them
    all, then re-run the pipeline.
  - Else (no marker, no orphans), `--force` has nothing to clean up —
    the pipeline runs from scratch and `--force` becomes equivalent
    to a normal invocation.
- Absent an `APPROVED` marker: fails fast with the exact human-readable
  reason, regardless of `--force`.
- Absent `tasks.md` in the change dir: fails fast. The critic loop
  guarantees it, but a direct invocation may hit an incomplete dir.

## `tasks.md` grammar (v1)

The critic loop's `tasks.md` is parsed as a flat list of numbered
sections, each with any number of bullet subtasks:

```
## Tasks

### 1. Section title
- [ ] Subtask line one
- [ ] Subtask line two

### 2. Another section title
- [ ] Yet another subtask
```

Rules:

- Top-level heading `## Tasks` is required. Missing → parse error.
- Section headings match `### <N>\. <title>`, with an ascending integer
  `N` starting at 1. Gaps (`### 1`, `### 3`) are a parse error.
- Each section MUST contain at least one `- [ ]` subtask line. Empty
  sections are a parse error (tasks.md should not land with no
  actionable content).
- Subtasks are preserved verbatim (including leading indentation of
  continuation lines) as the Beads task body.
- Anything outside a section (stray prose, extra headings) is ignored
  silently.
- Any non-markdown-link bracket expression on a section heading is
  treated as an intended marker. Unknown marker names (e.g.
  `[priority: 1]`) or malformed variants missing the colon (e.g.
  `[type impl]`) are parse errors — markers are a closed set.

### Explicit task markers

Two optional inline markers may appear on the section heading line,
space-separated, in square brackets:

```
### 2. [type: test] Implement critique parser tests
### 3. [blocked-by: 1, 2] Wire state machine
```

- `[type: impl | test | docs | spec]` — explicit task type. When absent,
  type is inferred from title keywords (see "Type inference" below).
  Unknown token → parse error.
- `[blocked-by: N]` or `[blocked-by: N, M, ...]` — explicit dependency
  on the named section numbers. When absent, each section implicitly
  depends on the previous section (`N` depends on `N-1`). Section 1 has
  no dependencies regardless of marker state. Self-reference
  (`blocked-by: 2` on section 2) is a parse error. Forward reference
  (`blocked-by: 3` on section 2) is a parse error.

## Type inference (no explicit marker)

Applied to the section title (markers removed, lowercased):

- Contains `test` or `tests` → `test`.
- Contains `doc` or `docs` or `readme` → `docs`.
- Contains `spec` or `specification` → `spec`.
- Otherwise → `impl`.

The inference is deterministic, not ML. A section titled
`"implement test runner primitives"` becomes type `test` under this
rule — operators who want `impl` for such a section should use the
explicit `[type: impl]` marker.

## Priority assignment

Section order is priority: section N has priority N. Beads treats lower
numeric priority as higher importance, which matches the ordering
convention in `tasks.md` (section 1 is done first).

No explicit priority marker in v1. A later iteration may add
`[priority: P]` if priority needs to diverge from order.

## Dependency edges

Default: each section depends on the previous section. Section 1 has no
edges.

Explicit override: `[blocked-by: N]` or `[blocked-by: N, M]` on a
section heading replaces the default entirely. An empty `[blocked-by: ]`
is a parse error; omit the marker to clear dependencies only on section
1 (which has none by default).

Dependency validation (parser rejects):

- Forward reference (depending on a later section).
- Self reference.
- Reference to a non-existent section number.
- Cycle introduced via explicit markers (shouldn't happen given the
  "no forward reference" rule, but the validator asserts acyclicity
  defensively).

## `BeadsAdapter` contract

Mirrors the existing authoring-backend pattern.

```python
class BeadsAdapter:
    def __init__(self) -> None: ...
        # validates shutil.which("bd") and raises PlanningError if
        # missing with a pip-install hint.

    def create_task(
        self,
        *,
        title: str,
        body: str,
        task_type: str,          # "impl" | "test" | "docs" | "spec"
        priority: int,           # 1-indexed, matches section order
        blocked_by_ids: list[str],
    ) -> str:                    # returns the new bd task id
        ...

    def close_task(self, task_id: str) -> None:
        ...

    def list_feature_tasks(self, feature: str) -> list[dict]:
        # lists tasks associated with this feature via the association
        # mechanism chosen during Task 2 (see "Body-writing mechanism"
        # below). Used for orphan preflight, --force teardown, and
        # diagnostic commands.
        ...
```

Invocations assemble the argv from the existing documented shape:

```
bd create --type=<t> --priority=<p> <body-writing flag/stdin> <title>
bd create --type=<t> --priority=<p> --blocked-by <id> <body-writing flag/stdin> <title>
bd close <id>
bd ls --json
```

The adapter parses `bd`'s JSON output on success. Non-zero exit raises
`PlanningError` with the `bd` stderr preserved.

### Body-writing mechanism

Each created Beads task MUST record two pieces of information beyond
its title:

1. Feature association: a first line of the form `feature: <name>`.
2. The ordered subtask list from the tasks.md section, preserved
   verbatim.

The `bd` README does not document how `bd create` accepts a multi-line
body (as of the current upstream commit). Task 2 is responsible for
determining the exact mechanism by running `bd create --help` and
adopting one of these in order of preference:

- `--description <text>` / `--body <text>` flag if present.
- Stdin if `bd create` reads stdin for body content.
- An editor-based flow invoked with `bd create --edit` (not usable by
  the adapter).

If none of the above works — i.e. `bd create` truly accepts only a
title — the adapter MUST fall back to the title-prefix convention:

- Title becomes `[feature:<name>] <original title>`.
- Subtask list is written to a per-feature Markdown file under
  `.beads/<feature>-subtasks.md`, committed alongside the `.beads/`
  state.
- `list_feature_tasks(feature)` filters on the title prefix.

Task 2 MUST document the chosen mechanism in the `BeadsAdapter` class
docstring so downstream readers see the decision without needing to
re-consult this design doc.

## Translation pipeline

1. Load change directory and gate on `APPROVED`.
2. Read and parse `tasks.md` into an ordered list of sections.
3. For each section, in ascending order:
   a. Compose the task title (remove markers from the heading).
   b. Compose the task body: `feature: <name>` + blank line + the
      verbatim subtask bullets of that section.
   c. Resolve `blocked_by_ids`: for each referenced section number,
      use the `bd` task id created earlier in this run.
   d. Invoke `BeadsAdapter.create_task(...)` and record the returned
      task id keyed by section number.
4. On the first adapter error mid-pipeline, abort. Already-created
   tasks are left in place as feature-tagged orphans (no
   `TRANSCRIBED.md` is written on failure). The command exits with a
   non-zero status and the error surfaced to the user. Partial state
   is resolvable via a manual `bd close` cycle or via a `--force`
   retry that uses the orphan-teardown path (see "Partial-failure
   rule" below).
5. On success, write `TRANSCRIBED.md` in the change directory with
   the feature name, timestamp, and the full list of created Beads
   task IDs for future `--force` teardown.

## Partial-failure rule

If the create loop fails mid-pipeline, `TRANSCRIBED.md` is NOT written
and the already-created Beads tasks remain in place as feature-tagged
orphans.

Re-invoking `turma plan-to-beads --feature X` without `--force`:

- Detects the orphans via `list_feature_tasks(feature)` during the
  preflight.
- Fails fast with a message listing the orphan IDs and the two
  recovery options below. It never silently duplicates.

Recovery options surfaced by the error:

1. Manual close, then retry fresh:
   ```
   bd close <id> <id> ...
   turma plan-to-beads --feature X
   ```
2. Automated close via `--force`:
   ```
   turma plan-to-beads --feature X --force
   ```
   With no `TRANSCRIBED.md`, `--force` falls back to
   `list_feature_tasks(feature)` to find and close the orphans before
   re-running the pipeline.

Automated full rollback that itself writes a partial-teardown marker
on failure is deferred; v1 prioritizes a clear, documented recovery
surface over hidden rollback logic.

## Teardown (used by `--force`)

`--force` picks the teardown source based on what is on disk:

- With `TRANSCRIBED.md` present:
  1. Read `TRANSCRIBED.md` and extract the recorded Beads task IDs.
  2. Call `BeadsAdapter.close_task(id)` for each, in reverse
     dependency order.
  3. Delete `TRANSCRIBED.md`.
- Without `TRANSCRIBED.md` but with orphans detected via
  `list_feature_tasks(feature)`:
  1. Close each orphan id in the order `list_feature_tasks` returns
     (Beads is tolerant of arbitrary close order; reverse dependency
     order is only relevant when the marker records it).
- With neither marker nor orphans: no teardown; the pipeline runs
  from scratch.

If any teardown close fails, abort before restarting the pipeline.

## `TRANSCRIBED.md` schema

```markdown
# TRANSCRIBED

- feature: <name>
- timestamp: 2026-04-22T00:00:00+00:00
- task_ids:
  - section 1: <bd-id>
  - section 2: <bd-id>
  - ...
```

Human-readable markdown; machine-parseable via a simple regex or a
dedicated parser. Task IDs are stored in section order.

## Error surface

All errors surface as `PlanningError` to keep consistency with the
existing CLI. Categories:

- `APPROVED` missing: `"plan-to-beads requires the plan to be approved first"`.
- `TRANSCRIBED.md` exists without `--force`: `"change already transcribed to Beads; use --force to re-create"`.
- Orphan feature-tagged tasks exist without `TRANSCRIBED.md` and without
  `--force`: `"feature-tagged tasks already exist in Beads from a prior
  failed transcription (ids: <ids>). Close them with 'bd close <ids>'
  or retry with --force."`
- `tasks.md` missing: `"tasks.md not found in openspec/changes/<feature>/"`.
- `tasks.md` parse failure: specific line-level reason.
- `bd` missing: `"bd CLI not found. Install it: pip install beads"`.
- `bd` non-zero exit: surfaces `bd` stderr verbatim.

## Open items deferred past v1

- Cross-epic dependencies and nested epics.
- Explicit priority markers `[priority: P]` when priority must diverge
  from section order.
- Automated partial-failure rollback.
- Beads integration of subtask-level acceptance criteria as structured
  Beads fields (v1 stores them verbatim in the task body).
- Web UI / Dolt-specific tuning.
