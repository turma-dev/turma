## Scope

Covers translation of an approved `tasks.md` into a feature-tagged task
set in Beads for a single Turma feature. Ends when the Beads tasks
exist and a `TRANSCRIBED.md` marker is written in the change directory.

**Terminology note.** Beads has a native "epic" concept (hierarchical
IDs like `bd-a3f8` / `bd-a3f8.1`) and a first-class `--parent` flag on
`bd create`. v1 deliberately does NOT use native Beads epics; instead
it records a feature association via a comma-separated
`feature:<name>` label on each Beads task (bd has first-class label
support and filters with `bd list --label feature:<name>`). Migrating
to native epics is a deferred open item — it would be a cleaner model,
but labels are sufficient for v1's orphan-detection and teardown
needs and require no pre-creation of a parent issue.

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

Parser priority is section order: the parser emits `priority = N` for
section N.

Beads priority is a 0-4 scale with 0 = highest. The transcription
pipeline (Task 3) maps parser priority to bd priority as
`min(N - 1, 4)`: section 1 → P0, section 2 → P1, … section 5 → P4,
section 6+ collapse to P4. This preserves ordering signal up to five
sections and degrades gracefully past that; dependency edges encode
the actual "do-first" ordering regardless.

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
        # missing with a `brew install beads` hint (Beads ships as a
        # Go binary via Homebrew; it is not a PyPI package).

    def create_task(
        self,
        *,
        title: str,
        description: str,           # full task body; multi-line OK
        bd_type: str,               # bd-native type: bug|feature|task|
                                    #   epic|chore|decision
        priority: int,              # bd-native 0-4 (0 = highest)
        feature: str,               # recorded as `feature:<name>` label
        extra_labels: tuple[str, ...] = (),
        blocker_ids: tuple[str, ...] = (),
    ) -> str:                       # returns the new bd task id
        ...

    def close_task(self, task_id: str) -> None:
        ...

    def list_feature_tasks(
        self, feature: str
    ) -> tuple[BeadsTaskRef, ...]:
        # lists OPEN tasks with label `feature:<name>`. Used for
        # orphan preflight, --force teardown, and diagnostic commands.
        # Returns `BeadsTaskRef(id, title, labels)` records.
        ...
```

Invocations (from the real `bd create --help` output as of Beads 1.0.2):

```
bd create --silent --type <T> --priority <0-4> \
          --description <body> --labels feature:<name>[,extra,...] \
          <title>
bd dep add <new-id> <blocker-id>               # per blocker
bd close <id>
bd list --label feature:<name> --json --limit 0
```

Non-zero exit raises `PlanningError` with `bd`'s stderr preserved
verbatim (or stdout if stderr is empty).

### Body-writing mechanism (settled)

Each Beads task records:

1. Feature association via a `feature:<name>` **label** (bd has first-
   class comma-separated labels; cleaner than a body-first-line tag
   and filters trivially with `bd list --label feature:<name>`).
2. The full description / subtask content via the inline
   `-d` / `--description <text>` flag. `subprocess.run` with a list
   argv handles multi-line text with no shell escaping.

Task 2 validated these against `bd create --help` (Beads 1.0.2) and
pinned the argv shape with unit tests so any future bd CLI drift
surfaces as a failing adapter test. The title-prefix and per-feature-
Markdown-file fallbacks originally enumerated here are not used.

### Dependency direction (settled)

`bd create --deps` uses *inverted* semantics relative to the direction
Turma's transcription pipeline produces: `--deps blocks:<id>` attaches
the new task as a **blocker** of `<id>`, meaning `<id>` depends on the
new task. Turma needs the opposite — the new task depends on the
listed blockers. The adapter therefore:

1. Creates the task with no `--deps`.
2. Runs `bd dep add <new-id> <blocker-id>` once per blocker. Per
   `bd dep add`'s documented semantics, "`bd dep add <blocked>
   <blocker>`" records that `<blocked>` depends on `<blocker>`, which
   is what we want.

## Translation pipeline

1. Load change directory and gate on `APPROVED`.
2. Read and parse `tasks.md` into an ordered list of sections.
3. For each section, in ascending order:
   a. Compose the task title (markers stripped from the heading).
   b. Compose the task description: the verbatim subtask bullets of
      that section. Feature association is carried on the task's
      `feature:<name>` label, not in the body.
   c. Translate parser-type → bd-type (`impl`/`test` → `task`,
      `docs` → `chore`, `spec` → `decision`). Include the original
      parser type as an extra `turma-type:<t>` label for downstream
      filtering.
   d. Translate priority: bd priority = `min(section_number - 1, 4)`
      (bd's scale is 0-4 with 0 = highest; section 1 → P0, sections
      6+ collapse to P4).
   e. Resolve `blocker_ids`: for each referenced section number, use
      the `bd` task id created earlier in this run.
   f. Invoke `BeadsAdapter.create_task(title=..., description=...,
      bd_type=..., priority=..., feature=..., extra_labels=...,
      blocker_ids=...)` and record the returned task id keyed by
      section number.
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
- `bd` missing: `"bd CLI not found. Install it with brew install beads (see https://github.com/steveyegge/beads for non-macOS paths)."`
- `bd` non-zero exit: surfaces `bd` stderr verbatim.

## Open items deferred past v1

- Cross-epic dependencies and nested epics.
- Explicit priority markers `[priority: P]` when priority must diverge
  from section order.
- Automated partial-failure rollback.
- Beads integration of subtask-level acceptance criteria as structured
  Beads fields (v1 stores them verbatim in the task body).
- Web UI / Dolt-specific tuning.
