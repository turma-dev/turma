## ADDED Requirements

### Requirement: bd version is checked before any Beads state is touched

When `BeadsAdapter` is constructed, it SHALL determine the installed bd
version by running `bd version --json` once, directly after confirming
`bd` is on PATH and before any other `bd` invocation. The version SHALL
be read from the `version` field of the JSON printed on stdout; stderr
from a successful invocation SHALL be ignored.

#### Scenario: version is the first bd call

- **WHEN** `BeadsAdapter()` is constructed with `bd` on PATH
- **THEN** `bd version --json` is the first and only `bd` subprocess
  run during construction

#### Scenario: bd missing

- **WHEN** `bd` is not on PATH
- **THEN** construction raises `PlanningError` with the install hint
  and runs no `bd` subprocess

#### Scenario: stderr noise on success

- **WHEN** `bd version --json` exits 0 with valid JSON on stdout and a
  warning on stderr
- **THEN** the version is taken from stdout and the stderr text doesn't
  affect the outcome

### Requirement: bd version strings are parsed with an explicit syntax

The parser SHALL accept `MAJOR.MINOR.PATCH`, with an optional leading
`v`, an optional pre-release suffix (`-` followed by dot-separated
identifiers), and an optional build-metadata suffix (`+` followed by
dot-separated identifiers), in that order. Any other string SHALL
count as undetermined. Build metadata SHALL be accepted and ignored for
every comparison.

#### Scenario: accepted forms

- **WHEN** the version is `1.0.2`, `v1.0.2`, `1.0.2+homebrew.1`,
  `1.3.1-rc.1`, or `1.3.1-rc.1+abc123`
- **THEN** it parses

#### Scenario: rejected forms

- **WHEN** the version is `dev`, `unknown`, an empty string, `1.0`, or
  `1.0.2.3`
- **THEN** it counts as undetermined

#### Scenario: build metadata is ignored

- **WHEN** the version is `1.0.2+anything`
- **THEN** it is classified exactly as `1.0.2`

### Requirement: installed bd is classified against the supported range

The supported range SHALL be defined by an inclusive minimum
`BD_MIN_VERSION` and an exclusive ceiling `BD_MAX_EXCLUSIVE`, initially
`1.0.2` and `1.1.0`. A parsed version SHALL be classified as follows:

- **below minimum:** it precedes `BD_MIN_VERSION` under semantic-version
  precedence, where a pre-release precedes its release (so `1.0.2-rc.1`
  is below minimum)
- **untested:** its release base (`MAJOR.MINOR.PATCH`, pre-release and
  build metadata removed) is at or above `BD_MAX_EXCLUSIVE` (so
  `1.1.0-rc.1` is untested, not in range)
- **in range:** neither of the above

#### Scenario: boundary table

- **WHEN** the installed version is each of the following
- **THEN** it is classified as shown:
  - `1.0.1` → below minimum
  - `1.0.2-rc.1` → below minimum
  - `1.0.2` → in range
  - `1.0.2+build.5` → in range
  - `1.0.9-rc.1` → in range
  - `1.0.99` → in range
  - `1.1.0-rc.1` → untested
  - `1.1.0-rc.1+abc` → untested
  - `1.1.0` → untested
  - `1.3.0` → untested

### Requirement: each classification has a defined outcome

Construction SHALL proceed or refuse according to the classification
and the `TURMA_ALLOW_UNTESTED_BD` environment variable. The override
SHALL take effect only when its value is exactly `1`. Refusal SHALL
raise `PlanningError`. Warnings SHALL be written to stderr only.

| Classification | Default | `TURMA_ALLOW_UNTESTED_BD=1` |
|---|---|---|
| in range | proceed silently | proceed silently |
| below minimum | refuse | refuse |
| untested | refuse | warn on stderr, proceed |
| undetermined | warn on stderr, proceed | warn on stderr, proceed |

The version is undetermined when `bd version --json` exits non-zero,
exceeds a 5-second timeout, prints invalid JSON, has no string
`version` field, or the field fails to parse.

#### Scenario: in range

- **WHEN** the installed version is `1.0.2`
- **THEN** construction succeeds and nothing is written to stdout or
  stderr

#### Scenario: below minimum ignores the override

- **WHEN** the installed version is `1.0.1` and
  `TURMA_ALLOW_UNTESTED_BD=1`
- **THEN** construction raises `PlanningError`

#### Scenario: untested refuses by default

- **WHEN** the installed version is `1.3.0` and
  `TURMA_ALLOW_UNTESTED_BD` is unset
- **THEN** construction raises `PlanningError` naming `1.3.0`, the
  supported range, and `TURMA_ALLOW_UNTESTED_BD=1`

#### Scenario: untested with the override

- **WHEN** the installed version is `1.3.0` and
  `TURMA_ALLOW_UNTESTED_BD=1`
- **THEN** construction succeeds and a warning naming `1.3.0` is
  written to stderr

#### Scenario: override value other than 1

- **WHEN** the installed version is `1.3.0` and
  `TURMA_ALLOW_UNTESTED_BD=true`
- **THEN** construction raises `PlanningError`

#### Scenario: undetermined version

- **WHEN** `bd version --json` exits non-zero, times out, prints invalid
  JSON, or reports `dev`
- **THEN** construction succeeds and a warning naming the supported
  range is written to stderr

#### Scenario: messages follow the constants

- **WHEN** `BD_MIN_VERSION` or `BD_MAX_EXCLUSIVE` is changed
- **THEN** the refusal and warning text show the new range with no
  other edit

### Requirement: commands report a refusal without touching Beads state

Each bd-using command SHALL exit 1 when construction refuses:
`turma plan-to-beads`, `turma run` (including `--dry-run`), and
`turma status`. It SHALL run no `bd` command other than
`bd version --json`. In `--json` modes the
refusal SHALL be reported through the command's existing error output,
and warnings SHALL NOT appear on stdout.

#### Scenario: plain output

- **WHEN** bd is below minimum or untested without the override, and
  the user runs `turma plan-to-beads`, `turma run`, or `turma status`
- **THEN** the command prints `error: <refusal message>` and exits 1

#### Scenario: run --json

- **WHEN** construction refuses during `turma run --json`
- **THEN** the refusal is emitted as a `turma.run.v1` `error` event and
  the command exits 1

#### Scenario: status --json

- **WHEN** construction refuses during `turma status --json`
- **THEN** stdout is a `turma.status.v1` object with an `error` field
  and the command exits 1

#### Scenario: warnings keep JSON stdout clean

- **WHEN** the version is undetermined, or untested with the override,
  during `turma run --json` or `turma status --json`
- **THEN** the warning appears on stderr and every stdout line remains
  valid JSON for that command's schema
