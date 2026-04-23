# `plan-to-beads` Smoke Procedure

Task 6 of `openspec/changes/beads-transcription/` closes out the change
set with an end-to-end validation against a real `bd` database. The
unit and pipeline suites (`tests/test_transcription_*.py`, 89 tests in
total) cover the parser, adapter argv shape, pipeline routing, and CLI
dispatch with subprocess stubs. This document is the complementary
manual smoke that exercises the real `bd` binary end-to-end.

## Prerequisites

- `bd` 1.0.2+ on PATH (`brew install beads` pulls Dolt as a
  dependency).
- A checked-out Turma repo with `uv sync` completed so `uv run turma`
  works.
- `jq` available for the verification commands below.

## Scratch setup

Point `TURMA_REPO` at your Turma checkout and run everything from a
scratch workdir so `bd` auto-discovers the scratch `.beads/` and
`turma plan-to-beads` reads `openspec/changes/smoke-demo/` from the
same cwd:

```bash
export TURMA_REPO="$(cd ~/coding_projects/turma && pwd)"
# Sanity-check that the repo venv has turma installed (expected
# after `uv sync`).
test -x "$TURMA_REPO/.venv/bin/turma" || (cd "$TURMA_REPO" && uv sync)

WORKDIR=$(mktemp -d)
cd "$WORKDIR"

# Minimum Turma project layout: config + role prompts.
cp "$TURMA_REPO/turma.example.toml" turma.toml
mkdir -p .agents openspec/changes/smoke-demo
cp "$TURMA_REPO/.agents/author.md" .agents/
cp "$TURMA_REPO/.agents/critic.md" .agents/

# Beads database (non-interactive to skip bd init's wizard).
BD_NON_INTERACTIVE=1 bd init --prefix smoke

# Pre-populate an approved change without running `turma plan`, so the
# smoke focuses on transcription rather than LLM-driven planning.
cat > openspec/changes/smoke-demo/tasks.md <<'EOF'
## Tasks

### 1. Extract primitives
- [ ] Split the module
- [ ] Add an injection seam

### 2. Write tests
- [ ] Cover happy paths

### 3. Update the README
- [ ] Document the feature
EOF
printf '## Why\nStub.\n'   > openspec/changes/smoke-demo/proposal.md
printf '## Goals\nStub.\n' > openspec/changes/smoke-demo/design.md
touch openspec/changes/smoke-demo/APPROVED
```

`bd init` may take 30-90 seconds on first run — Dolt initializes a
SQL-compatible workspace and the non-interactive wizard still exercises
the same path as the interactive flow.

## Step 1 — Happy path

Invoke the Turma binary from the repo venv while staying in `$WORKDIR`
so `turma` finds `openspec/changes/smoke-demo/` and `bd` auto-discovers
`.beads/smoke.db`:

```bash
cd "$WORKDIR"
"$TURMA_REPO/.venv/bin/turma" plan-to-beads --feature smoke-demo
```

Expected stdout:

```
feature: smoke-demo
marker:  .../openspec/changes/smoke-demo/TRANSCRIBED.md
tasks:
  section 1: bd-smoke-1
  section 2: bd-smoke-2
  section 3: bd-smoke-3
```

Verify the feature-tagged tasks exist and carry the expected labels:

```bash
cd "$WORKDIR"
bd list --label feature:smoke-demo --json --limit 0 \
  | jq '[.[] | {id, title, labels, issue_type: .type}]'
```

Expected (order may vary; ids depend on bd's prefix):

```json
[
  {"id": "bd-smoke-1", "title": "Extract primitives",
   "labels": ["feature:smoke-demo", "turma-type:impl"],
   "issue_type": "task"},
  {"id": "bd-smoke-2", "title": "Write tests",
   "labels": ["feature:smoke-demo", "turma-type:test"],
   "issue_type": "task"},
  {"id": "bd-smoke-3", "title": "Update the README",
   "labels": ["feature:smoke-demo", "turma-type:docs"],
   "issue_type": "chore"}
]
```

Dependency edges: section 2 should block-by section 1, section 3 by
section 2.

```bash
bd dep list bd-smoke-2
bd dep list bd-smoke-3
```

Each should report a `blocks` relationship toward the predecessor task.

`TRANSCRIBED.md` should record the created ids:

```bash
cat openspec/changes/smoke-demo/TRANSCRIBED.md
```

Expected shape:

```markdown
# TRANSCRIBED

- feature: smoke-demo
- timestamp: <ISO-8601 UTC>
- task_ids:
  - section 1: bd-smoke-1
  - section 2: bd-smoke-2
  - section 3: bd-smoke-3
```

## Step 2 — `--force` replay against `TRANSCRIBED.md`

```bash
cd "$WORKDIR"
"$TURMA_REPO/.venv/bin/turma" plan-to-beads --feature smoke-demo --force
```

Expected behavior:

- The three recorded ids are closed in reverse section order
  (`bd-smoke-3`, then `bd-smoke-2`, then `bd-smoke-1`). Confirm with
  `bd list --all --label feature:smoke-demo --json` — the old rows
  should show `status: closed` and three new rows should be open.
- `TRANSCRIBED.md` is re-written with the fresh ids.

## Step 3 — `--force` replay against orphans (no marker)

Simulate a crashed prior run:

```bash
cd "$WORKDIR"
rm openspec/changes/smoke-demo/TRANSCRIBED.md
```

Run without `--force` — should refuse with the orphan ids:

```bash
"$TURMA_REPO/.venv/bin/turma" plan-to-beads --feature smoke-demo
# error: feature-tagged tasks already exist in Beads from a prior
# failed transcription (ids: bd-smoke-4, bd-smoke-5, bd-smoke-6).
# Close them with `bd close bd-smoke-4 bd-smoke-5 bd-smoke-6` or retry with --force.
```

Re-run with `--force`:

```bash
"$TURMA_REPO/.venv/bin/turma" plan-to-beads --feature smoke-demo --force
```

Should close the feature-tagged orphans and create fresh tasks.

## Step 4 — Malformed `TRANSCRIBED.md` hard-reject

Corrupt the marker and re-run with `--force`:

```bash
cd "$WORKDIR"
printf '# TRANSCRIBED\n\n- task_ids:\n  (corrupted)\n' \
  > openspec/changes/smoke-demo/TRANSCRIBED.md

"$TURMA_REPO/.venv/bin/turma" plan-to-beads --feature smoke-demo --force
# error: TRANSCRIBED.md at ... exists but no `- section N: <id>` lines
# could be parsed. Cannot determine what to tear down. Inspect the
# file, delete it manually, or close feature-tagged tasks with
# `bd close` and retry.
```

The marker must remain on disk after this failure — no tasks should be
closed or created.

## Cleanup

```bash
rm -rf "$WORKDIR"
```

## Failure-signature cheat sheet

- `error: plan-to-beads requires the plan to be approved first` — no
  `APPROVED` marker in the change dir.
- `error: bd CLI not found. Install it with `brew install beads`` —
  `bd` is not on PATH in the shell that invoked Turma.
- `error: tasks.md parse failure: …` — the parser rejected the
  `tasks.md` shape (most commonly: missing `## Tasks` header,
  non-ascending section numbers, empty section, unknown bracket
  marker).
- `error: change already transcribed to Beads; use --force to re-create`
  — happy-path idempotency guard fired. Confirm by `cat
  openspec/changes/<feature>/TRANSCRIBED.md`.

## Notes for future Task 7+ wiring

- `bd init` has an interactive wizard by default. Set
  `BD_NON_INTERACTIVE=1` (or pass `--non-interactive`) in any
  automation that needs to spin up a fresh database.
- `bd list`'s default `--limit` is 50. Always pass `--limit 0` when
  filtering orphans for deterministic output.
- The Beads Dolt DB lives at `.beads/*.db`. It is binary and is
  expected to be ignored by `.gitignore`'s `*.db` rule; the
  git-trackable export is `.beads/issues.jsonl`, auto-written by `bd`
  after each write.
