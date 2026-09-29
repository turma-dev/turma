## Why

Turma drives `bd` (Beads) as a subprocess and depends on specific bd
behaviour: the argv and JSON shapes in `BeadsAdapter`, when bd exports
and stages `.beads/issues.jsonl`, and how bd's git hooks interact with
Turma's code-only task commits. All of it was verified against **bd
1.0.2**. The adapter's comments say so ("Verified against bd 1.0.2"),
and `docs/architecture.md` cites a 1.0.2-specific hook bug.

Turma checks only that `bd` is on PATH (`shutil.which("bd")` in
`BeadsAdapter.__init__`). It never checks the version. Upstream is now
at bd 1.3.0, and releases since 1.0.2 change behaviour Turma relies on:

- **1.0.3** fixes the worktree hook bug (steveyegge/beads#3311) that
  Turma's `core.hooksPath=/dev/null` bypass partly works around.
- **1.0.5** makes JSONL auto-export and auto-staging opt-in
  (`export.auto` and `export.git-add` now default to `false`). The
  merge-cleanliness revert-after-mutation logic and the bd-state-clean
  preflight were written against the old default, where every mutation
  exported and staged.
- **1.3.0** runs schema migrations the first time it opens a store.

`brew install beads` installs whatever version is current, so a new
user gets 1.3.0 today: a version Turma has never been run against,
with no warning. If a behaviour change breaks something, it surfaces
mid-run as a confusing git or bd error rather than at startup as a
clear one.

## What Changes

Turma declares the bd versions it supports and checks the installed bd
against them before touching any Beads state.

- `BeadsAdapter.__init__`, the single point every bd-using command goes
  through (`plan-to-beads`, `run` including `--dry-run`, and `status`),
  runs `bd version --json` once after the existing PATH check and parses
  `version`.
- **Below the minimum:** refuse with a `PlanningError` that names the
  installed version, the supported range, and how to upgrade.
- **Untested (release base at or above the ceiling, so pre-releases of
  an untested version count too):** refuse by default,
  naming the version and the tested range. An explicit opt-out,
  `TURMA_ALLOW_UNTESTED_BD=1`, turns the refusal into a warning on
  stderr and proceeds.
- **Version can't be determined** (non-zero exit, unparseable output,
  a dev build): warn on stderr and proceed. An unknown version means
  "we don't know", not "this is broken".
- The supported range lives in two constants in
  `src/turma/transcription/beads.py`, so raising it after a successful
  smoke test is a one-line change plus docs.

The initial range is **≥ 1.0.2, < 1.1.0**: exactly what Turma has been
verified against. Raising the ceiling to cover 1.3.0 is a separate
change, gated on a live smoke run (see Out of Scope).

## Guardrails

- **No Beads state is touched before the check.** The check runs in the
  constructor, before any `bd` mutation or read of task state, so a
  refused version leaves the repo exactly as it was.
- **One extra subprocess per command, not per bd call.** The adapter is
  constructed once per CLI invocation.
- **Default behaviour for users on the tested version doesn't change:**
  no new output, no new prompts.
- **`--json` modes stay well-formed.** A refusal goes through the
  existing `PlanningError` path each command already renders (a plain
  `error:` line, the `turma.status.v1` error object, or a
  `turma.run.v1` `error` event). Warnings go to stderr only, never into
  a JSON stream on stdout.

## Capabilities

### Modified Capabilities

- **Beads adapter construction** (`beads-transcription`): validates the
  bd version as well as its presence. The spec delta
  (`specs/beads-transcription/spec.md`) uses `ADDED` requirements,
  because the capability has no base spec in `openspec/specs/` yet (see
  the design's Spec delta section).

## Impact

- `src/turma/transcription/beads.py`: version constants, a pure
  version parser and comparator, the check in `__init__`, updated
  install hint.
- Tests: `tests/test_transcription_beads.py` (subprocess-mocked unit
  layer), plus a real-`bd` integration test that skips when `bd` is
  absent.
- Docs: `README.md` (Beads install section and prerequisites list gain
  the supported range and the opt-out), `docs/architecture.md` (a short
  note where the Beads adapter is described).

## Out of Scope

- **Raising the ceiling to 1.3.0.** That needs a live pooled smoke run
  on 1.3.0 first, because of the 1.0.5 export-default change and 1.3.0's
  migrations. It lands as its own change once the smoke passes.
- **Removing the `core.hooksPath=/dev/null` bypass.** It's now
  load-bearing for keeping the export out of task commits, whatever the
  bd version (`swarm-bd-export-serialization`).
- **Checking the Dolt CLI version.** Turma's stores run bd in embedded
  mode, where the Dolt engine is compiled into bd and the `dolt` binary
  on PATH isn't on Turma's code path.
- **Pinning or installing bd.** Turma validates the version; it doesn't
  manage it.
