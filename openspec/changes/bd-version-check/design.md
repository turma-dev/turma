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

- accepts `MAJOR.MINOR.PATCH` with an optional leading `v` and an
  optional pre-release suffix (`1.3.1-rc.1`)
- returns `None` for anything else (`dev`, `unknown`, empty)

Ordering: compare `(major, minor, patch)`. A pre-release sorts
**below** its release (`1.1.0-rc.1 < 1.1.0`). Build metadata is
ignored.

Constants in `beads.py`:

```python
BD_MIN_VERSION = (1, 0, 2)      # inclusive
BD_MAX_EXCLUSIVE = (1, 1, 0)    # first untested version
```

The ceiling is exclusive and set at a minor boundary, because upstream
shipped behaviour changes in patch releases within 1.0.x (1.0.3, 1.0.5).
That's a known imprecision: 1.0.3–1.0.5 fall inside the range but
haven't been smoke-tested. See Open Questions.

## Outcomes

| Installed | Default | `TURMA_ALLOW_UNTESTED_BD=1` |
|---|---|---|
| in range | proceed silently | proceed silently |
| below `BD_MIN_VERSION` | refuse | refuse (no override) |
| ≥ `BD_MAX_EXCLUSIVE` | refuse | warn on stderr, proceed |
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
stdout, so a `--json` consumer's stream stays parseable.

## Testing strategy

Two layers, test-first per acceptance criterion:

1. **Unit (subprocess mocked).** Patch `subprocess.run` and
   `shutil.which` in `turma.transcription.beads`:
   - the parser table: release, `v` prefix, pre-release,
     garbage → `None`
   - ordering: pre-release below release, boundaries inclusive and
     exclusive as specified
   - each row of the outcomes table, including the override set to
     `1`, set to `true` (no effect), and unset
   - a non-zero exit, a timeout, and invalid JSON each count as
     undetermined
   - the message renders the range from the constants
   - existing adapter tests still construct the adapter: the shared
     fixtures gain a default in-range `bd version --json` response
2. **Real tool (skipped without `bd`).** Construct `BeadsAdapter()`
   against the real `bd` on PATH and assert that the installed version
   parses (not `None`). This catches upstream changes to the
   `version --json` shape without asserting which version is installed.

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

## Open questions (for review)

1. **Ceiling granularity.** `< 1.1.0` admits 1.0.3–1.0.5, which include
   the 1.0.5 export-default flip and haven't been smoke-tested. The
   alternative is a patch-exact ceiling (`< 1.0.3`), which is stricter
   but refuses the upstream fix for #3311. Recommendation: keep
   `< 1.1.0` and let the follow-up 1.3.0 smoke settle the range for
   real; the reviewer may prefer strict.
2. **Override name.** `TURMA_ALLOW_UNTESTED_BD` follows no existing
   Turma convention, because Turma has no user-facing env vars yet. A
   `[beads] allow_untested = true` key in `turma.toml` is the
   alternative. An env var is proposed because it's per-shell and
   deliberate, and it doesn't linger in a config file after the ceiling
   is raised.
3. **Install hint URL.** `BEADS_INSTALL_HINT` and the README link to
   `github.com/steveyegge/beads`, and upstream now lives at
   `github.com/gastownhall/beads`. Fixing that link is trivial and is
   folded into Task 3 unless the reviewer wants it split out.
