## Scope

Covers translation of an approved `tasks.md` into a Beads epic for a single
Turma feature. Ends when the Beads tasks exist and a `TRANSCRIBED.md`
marker is written in the change directory.

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

Idempotency is human-supervised. A `TRANSCRIBED.md` marker written after
successful transcription is the sole signal that this change has already
been transcribed. A retry requires `--force` AND the prior marker; forcing
without a prior marker is rejected (prevents silently recreating tasks for
a change that was never transcribed on this machine).

## Command surface

```
turma plan-to-beads --feature <name>
turma plan-to-beads --feature <name> --force
```

Behavior:

- Without `--force`: fails fast if `TRANSCRIBED.md` already exists.
- With `--force`: requires `TRANSCRIBED.md` to exist; invokes a tear-down
  pass that closes the previously-created Beads tasks (see "Teardown"
  below) and then runs transcription from scratch.
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
- Anything outside a section (stray prose, extra headings, markers on
  their own line) is ignored for task creation but preserved in the
  parser's trace for diagnostic messages.

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

    def list_epic(self, feature: str) -> list[dict]:
        # lists tasks associated with this feature; used for --force
        # teardown and for diagnostic commands.
        ...
```

Invocations assemble the argv from the existing documented shape:

```
bd create --type=<t> --priority=<p> <title>
bd create --type=<t> --priority=<p> --blocked-by <id> <title>
bd close <id>
bd ls --json
```

The adapter parses `bd`'s JSON output on success. Non-zero exit raises
`PlanningError` with the `bd` stderr preserved.

Feature-to-task association: Turma tags each created task with the
feature name in the task body (first line: `feature: <name>`). The
adapter's `list_epic` filters on this tag. Beads itself does not
require a feature concept; this tagging is a Turma convention that
allows `--force` to find prior tasks.

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
   tasks are left in place; the command exits with a non-zero status
   and the error surfaced to the user. Partial state is resolvable
   manually via `bd` or with a fresh `--force` retry once
   `TRANSCRIBED.md` has been written (see "Partial-failure rule"
   below).
5. On success, write `TRANSCRIBED.md` in the change directory with
   the feature name, timestamp, and the full list of created Beads
   task IDs for future `--force` teardown.

## Partial-failure rule

If step 3 fails mid-pipeline, `TRANSCRIBED.md` is NOT written.
Re-invoking `turma plan-to-beads --feature X` without `--force` will
fail at the idempotency check only if `TRANSCRIBED.md` exists; since
it does not on partial failure, the retry without `--force` runs from
scratch AND creates duplicates of the already-created tasks from the
prior attempt. The documented workaround is:

```
# Inspect any orphan tasks from the crashed attempt.
bd ls --json | jq '.[] | select(.body | contains("feature: X"))'

# Manually close them.
bd close <id>...

# Retry.
turma plan-to-beads --feature X
```

A fully automated partial-failure recovery is deferred; v1 prioritizes
a clear, documented manual path over hidden rollback logic.

## Teardown (used by `--force`)

With `--force` and a `TRANSCRIBED.md` present:

1. Read `TRANSCRIBED.md` and extract the recorded Beads task IDs.
2. Call `BeadsAdapter.close_task(id)` for each, in reverse dependency
   order.
3. Delete `TRANSCRIBED.md`.
4. Fall through to the normal transcription pipeline.

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
- `--force` without `TRANSCRIBED.md`: `"--force requires TRANSCRIBED.md to exist"`.
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
