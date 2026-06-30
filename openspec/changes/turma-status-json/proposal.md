## Why

`turma status --feature <name>` prints a human-readable text readout. The
`turma-status` design explicitly deferred a machine-readable mode
(`openspec/changes/turma-status/design.md`: "Machine-readable output
(`--json`). v1 is text only; JSON is [deferred]"). This change delivers that
deferred mode.

Operators and scripts that want to gate on feature state — CI checks,
dashboards, "is everything merged yet?" polling — currently have to scrape the
text output, which is brittle and was never meant to be parsed. A stable JSON
payload makes `turma status` consumable by tooling without that fragility.

The readout is already structured internally: `status_readout` gathers the full
state into typed intermediates (`_Preflight`, `_Buckets`, task lists, retry map,
PR-state map, branches, PR summaries) and only then flattens them to text in
`_render`. `--json` is a second renderer over the *same gathered data* — no new
adapter reads, no new I/O, and the read-only invariant is preserved unchanged.

## What Changes

- **New CLI flag:** `turma status --feature <name> --json`. Absent the flag,
  the text output is byte-for-byte unchanged.
- **`status.py` gains a JSON renderer alongside the text one.** The adapter
  read pipeline (the "gather" half of `status_readout`) is factored so both
  renderers consume one gathered model; the gather order and the no-mutation
  invariant are identical to today.
- **The JSON payload mirrors the text sections 1:1**, with a stable top-level
  `"schema": "turma.status.v1"` identifier. Shape:

```json
{
  "schema": "turma.status.v1",
  "feature": "oauth",
  "spec": { "change_dir": "openspec/changes/oauth/", "present": true,
            "approved": true, "transcribed": false },
  "tasks": { "ready": 1, "in_progress": 2, "blocked_deferred": 0,
             "closed": 3, "needs_human_review": 0 },
  "ready": [ { "id": "bd-4", "title": "Wire callback" } ],
  "in_progress": [
    {
      "id": "bd-2", "title": "Token exchange",
      "retries": 0, "max_retries": 1,
      "worktree": { "present": true, "path": "/abs/.worktrees/oauth/bd-2" },
      "sentinel": { "status": "failed", "reason": "timeout after 1800s" },
      "pr": { "number": 7, "state": "OPEN", "url": "https://.../pull/7" }
    }
  ],
  "pull_requests": [ { "number": 7, "url": "...", "state": "OPEN",
                       "title": "...", "head_branch": "task/oauth/bd-2" } ],
  "orphan_branches": [ "task/oauth/bd-old" ]
}
```

- **Nullability, pinned:** `pr` is `null` when the task carries no `turma-pr:`
  label; `sentinel.status`/`sentinel.reason` are `null` when no sentinel /
  not-failed; `worktree.path` is `null` when the worktree is absent.
- **Pretty-printed** with `indent=2` and deterministic key/list ordering
  matching the text section order. Operators who want compact pipe through
  `jq -c`.
- **Errors still raise.** A failing adapter read raises `PlanningError` exactly
  as in the text path — no partial JSON blob is ever emitted.

## Capabilities

### Modified Capabilities

- `turma-status`: gains a `--json` machine-readable rendering of the existing
  readout. The text rendering, the adapter read pipeline, and the no-mutation
  invariant are unchanged; JSON is an additive second view of the same gathered
  state.

## Impact

- **New files:** none.
- **Modified files:**
  - `src/turma/swarm/status.py` — factor gather → model; add `_render_json`;
    `status_readout` (or a thin sibling) selects the renderer.
  - `src/turma/cli.py` — `status` subparser gains `--json`; the `status`
    branch passes it through.
  - `tests/test_swarm_status.py` — JSON-mode tests: shape/sections, nullability
    cases, schema field, `--json` still asserts zero mutating adapter calls,
    text output unchanged when flag absent.
  - `README.md` — Feature Status section documents `--json`.
  - `docs/architecture.md` — one line noting the machine-readable mode (the
    status section, no state-machine change).
  - `CHANGELOG.md` `[Unreleased]`.
- **No new runtime deps.** `json` is stdlib. No new adapter methods, no new
  `bd`/`gh` calls.

## Out of Scope

- `--json` for `turma plan` / `turma run`. This change sets the house style but
  implements it only for `status`.
- Schema **versioning machinery** (negotiation, multiple supported versions,
  deprecation). The single `"schema": "turma.status.v1"` string is a stable
  identifier, not a versioning system; bumping it later is a future decision.
- Streaming / NDJSON / per-event output.
- Any change to the text output, its sections, or their ordering.
- New fields beyond the 1:1 mirror (e.g. timestamps, durations, commit SHAs).
  Additive fields are a later change with a concrete consumer.
