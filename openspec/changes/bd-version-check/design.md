# Design: bd version check

## Where the check lives

Every bd-using command constructs exactly one `BeadsAdapter`:

- `turma plan-to-beads`: `cli.py`, `adapter = BeadsAdapter()`
- `turma run` and `turma status`: `build_services` in
  `swarm/_orchestrator.py` (`beads=BeadsAdapter()`); `status.py`
  consumes the same `SwarmServices`

`BeadsAdapter.__init__` already raises `PlanningError(BEADS_INSTALL_HINT)`
when `bd` isn't on PATH, and each command already maps `PlanningError`
to exit 1 before touching Beads state. The version check goes directly
after the PATH check, so it inherits that ordering and error handling
with no new plumbing in the CLI.

`turma init` and `turma plan` don't construct the adapter, so they keep
working without bd, as they do today.

## Reading the version

Use `bd version --json`. Both bd 1.0.2 and 1.3.0 emit a JSON object
with a `version` key (plus `build` and `branch`):

```
$ bd version --json
{
  "branch": "v1.0.2",
  "build": "Homebrew",
  "version": "1.0.2"
}
```

The plain form (`bd version 1.0.2 (Homebrew)`) varies with build
metadata (`(Build: branch@commit)`, `(Build: commit)`, `(Build)`), so
JSON is the stable surface. There is no fallback to parsing the text
form: if the JSON form fails, the version counts as undetermined (see
below).

`bd version` may print a "multiple 'bd' binaries found in PATH" warning
on stderr. The check reads stdout only and ignores stderr on success.

Run with a short timeout (5s). A hang counts as undetermined, not a
refusal.

## Parsing and comparing

A pure function, `parse_bd_version(text) -> BdVersion | None`:

- accepts `MAJOR.MINOR.PATCH`, with an optional leading `v`, an
  optional pre-release suffix (`-rc.1`), and an optional build-metadata
  suffix (`+homebrew.1`), in that order
- returns `None` for anything else (`dev`, `unknown`, empty, `1.0`,
  `1.0.2.3`)

Build metadata is accepted by the syntax and ignored by every
comparison: `1.0.2+x` is classified exactly as `1.0.2`.

Pre-releases are handled differently at each bound, deliberately:

- **Minimum:** semantic-version precedence, where a pre-release
  precedes its release. `1.0.2-rc.1` is **below minimum**: it's a build
  from before the version Turma was verified against.
- **Ceiling:** compare the release base only (`MAJOR.MINOR.PATCH` with
  the pre-release removed). `1.1.0-rc.1` is **untested**, not in range.
  Plain precedence would put it below `1.1.0` and admit a 1.1 build
  nobody has tested; comparing the base closes that gap.

Boundary table, used verbatim as the parameterized test:

| Version | Classification |
|---|---|
| `1.0.1` | below minimum |
| `1.0.2-rc.1` | below minimum |
| `1.0.2` | in range |
| `1.0.2+build.5` | in range |
| `1.0.9-rc.1` | in range |
| `1.0.99` | in range |
| `1.1.0-rc.1` | untested |
| `1.1.0-rc.1+abc` | untested |
| `1.1.0` | untested |
| `1.3.0` | untested |

Constants in `beads.py`:

```python
BD_MIN_VERSION = (1, 0, 2)      # inclusive
BD_MAX_EXCLUSIVE = (1, 1, 0)    # first untested version
```

The ceiling is exclusive and set at a minor boundary, because upstream
shipped behaviour changes in patch releases within 1.0.x (1.0.3, 1.0.5).
That's a known imprecision: 1.0.3–1.0.5 fall inside the range but
haven't been smoke-tested. See Decisions.

## Outcomes

| Installed | Default | `TURMA_ALLOW_UNTESTED_BD=1` |
|---|---|---|
| in range | proceed silently | proceed silently |
| below minimum (precedes `BD_MIN_VERSION`) | refuse | refuse (no override) |
| untested (release base ≥ `BD_MAX_EXCLUSIVE`) | refuse | warn on stderr, proceed |
| undetermined | warn on stderr, proceed | warn on stderr, proceed |

**Too old has no override.** Turma's adapter depends on argv and JSON
shapes that 1.0.2 introduced or that it was verified against, so an
older bd fails in unclear ways partway through.

**Too new is overridable.** A newer bd is the common case for fresh
installs (`brew install beads` ships 1.3.0 today), and it may well
work. Refusing by default makes the untested state visible. The opt-out
lets someone who has accepted the risk proceed without waiting for a
Turma release.

Only the exact value `1` enables the override. Any other value,
including `true`, leaves the refusal in place. That keeps the rule
unambiguous, and the error message names the exact value.

## Messages

Refusal (too new):

```
bd 1.3.0 is newer than the versions Turma has been tested with
(>= 1.0.2, < 1.1.0). Install a supported bd, or set
TURMA_ALLOW_UNTESTED_BD=1 to run anyway.
```

Refusal (too old):

```
bd 1.0.1 is older than Turma supports (>= 1.0.2, < 1.1.0).
Upgrade bd: `brew upgrade beads`, or see the Beads install docs.
```

Undetermined (stderr):

```
warning: could not determine the bd version (<reason>); Turma is tested
with bd >= 1.0.2, < 1.1.0.
```

The range in every message is rendered from the constants, never
hard-coded, so raising the range can't leave a stale message behind.

## JSON modes

`plan-to-beads` has no JSON mode. `run --json` already reports a failure
during service construction as a `turma.run.v1` `error` event, and
`status --json` as a `turma.status.v1` error object (`_run_error` and
`_status_error` in `cli.py`). The refusal reuses those paths, so no new
schema fields are introduced. Warnings go to stderr and never enter
stdout, so both output shapes stay parseable: `run --json` is
newline-delimited JSON, so every stdout line must parse on its own;
`status --json` is one indented, multi-line object (`json.dumps(...,
indent=2)`), so the whole of stdout must parse as one document.

## Testing strategy

Two layers, test-first per acceptance criterion:

1. **Unit (subprocess mocked).** Patch `subprocess.run` and
   `shutil.which` in `turma.transcription.beads`:
   - the parser table: release, `v` prefix, pre-release, build
     metadata, pre-release plus build metadata; garbage → `None`
   - the boundary table above, parameterized
   - each row of the outcomes table, including the override set to
     `1`, set to `true` (no effect), and unset
   - a non-zero exit, a timeout, and invalid JSON each count as
     undetermined
   - the message renders the range from the constants
   - existing adapter tests still construct the adapter: the shared
     fixtures gain a default in-range `bd version --json` response
2. **Real tool (skipped without `bd`).** Run `bd version --json`
   directly (not through the adapter) and assert that the output parses
   and has a string `version` field that `parse_bd_version` accepts.
   This catches upstream changes to the `version --json` shape without
   assuming which version is installed. A second assertion constructs
   `BeadsAdapter()` and checks that the result matches the
   classification of that same parsed version: success when in range,
   `PlanningError` when below minimum or untested (with the override
   unset in the test environment). The test therefore passes on any
   installed bd, including one newer than the ceiling.

## Rejected alternatives

- **Parse the plain `bd version` text.** Its format varies with build
  metadata, and JSON is available in every supported version.
- **Warn instead of refusing on too-new, like Beads' own Dolt version
  check.** Beads can afford a warning because the Dolt CLI is optional
  for most of its modes. bd is on every Turma code path that touches
  task state, and the known 1.0.5 default change hits the
  merge-cleanliness contract directly. Refusal with an explicit opt-out
  keeps the risk visible.
- **Check once per bd call.** It costs a subprocess per call, and the
  binary doesn't change mid-run.
- **Check in the CLI layer.** Three call sites instead of one, and a
  future caller that constructs the adapter directly would skip it.

## Decisions (resolved in review)

1. **Ceiling stays `< 1.1.0`.** It admits 1.0.3–1.0.5, which haven't
   been smoke-tested (including the 1.0.5 export-default flip), but it
   keeps the 1.0.3 upstream fix for #3311. A patch-exact `< 1.0.3` would
   refuse that fix. The planned 1.3.0 smoke run is where support gets
   widened.
2. **The override is the env var `TURMA_ALLOW_UNTESTED_BD=1`,** not a
   `turma.toml` key. It's deliberate and per-shell, and it doesn't
   linger in a config file after the ceiling is raised.
3. **The Beads links move to `github.com/gastownhall/beads` in this
   change** (Task 3.5). `BEADS_INSTALL_HINT` and the README still point
   at the old `steveyegge/beads` location, and the fix is user-facing
   maintenance directly tied to installing bd.
4. **Pre-releases at the ceiling are untested.** `1.1.0-rc.1` is
   classified by its release base, so it can't slip into the range by
   sorting below `1.1.0` (see Parsing and comparing).

## Spec delta

`openspec/specs/` has no `beads-transcription` spec yet, and OpenSpec
allows only `ADDED` requirements against a capability with no existing
spec. `MODIFIED` and `RENAMED` require one. So the delta in
`specs/beads-transcription/spec.md` uses `## ADDED Requirements`, even
though the behaviour it describes modifies an existing adapter.
