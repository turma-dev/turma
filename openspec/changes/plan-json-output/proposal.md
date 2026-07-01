## Why

The operator API surface is nearly complete but has one gap:

- `turma status --json` — a `turma.status.v1` **snapshot** of current feature state.
- `turma run --json` — a `turma.run.v1` **NDJSON event stream** of live swarm execution.
- `turma plan` — **text / human-gate only.** No machine-readable form.

`plan` is the one command a surface (a future MCP client, a dashboard) cannot
consume structurally. Its whole job is the author/critic loop and the human
approval gate, so a surface that wants to *show* or *drive* planning has to
scrape human strings like `planning paused at round 2` and `state: approved`.
This change closes that gap and completes the surface's read side.

Unlike `run`, `plan` is **not a live stream** — each invocation (fresh plan or
resume) runs the state machine to a single terminal-or-suspended outcome and
reports it. So the right shape is a **snapshot JSON object**, mirroring
`status --json`, not an NDJSON event stream. (Per-round streaming would need the
state machine to emit progress mid-loop — out of scope; see below.)

## What Changes

- **New flag:** `turma plan --feature <name> --json` and
  `turma plan --feature <name> --resume [--approve|--revise|--abandon|--override]
  --json` emit a single `turma.plan.v1` JSON snapshot of the outcome instead of
  the text lines. Absent the flag, text output is **byte-for-byte unchanged**.
- **Snapshot payload** describes the planning outcome — the gate/terminal
  `state`, the `round`, the `checkpoint`, the suspended `next_nodes`, the
  `artifacts_dir`, and (resume only) the `action` taken:

```json
{
  "schema": "turma.plan.v1",
  "feature": "oauth",
  "state": "awaiting_human_approval",
  "round": 2,
  "next_nodes": ["human_approval"],
  "checkpoint": ".langgraph/oauth.sqlite",
  "artifacts_dir": "openspec/changes/oauth/",
  "action": null
}
```

- **`state`** is one of the committed planning states:
  `awaiting_human_approval`, `approved`, `needs_revision`, `abandoned`,
  `needs_human_review`.
- **`action`** (resume invocations only) is the `ResumeAction`:
  `status`, `approve`, `revise`, `abandon`, `override_approve`; `null` for a
  fresh `plan`.
- **Errors** surface as JSON in `--json` mode: on `PlanningError`, a single
  `{"schema": "turma.plan.v1", "error": "<message>"}` object, exit nonzero — so
  a consumer always gets JSON, never a bare `error: <msg>` text line. (Recommended
  over the current `status --json` behavior, which still emits text on error; see
  design "Error shape" for the trade-off.)

## Capabilities

### Modified Capabilities

- `critic-loop` / planning CLI: `turma plan` (and its resume surface) gain a
  `--json` snapshot rendering. The author/critic loop, the state machine, the
  human gate, and the text output are unchanged; JSON is a second rendering of
  the same terminal `PlanningGraphResult`.

## Impact

- **New files:** none.
- **Modified files:**
  - `src/turma/planning/__init__.py` — `run_planning` factored so the terminal
    `PlanningGraphResult` is available to render as text or JSON, **and all
    per-node progress prints** (drafting `generating …`/`done`, critic
    `status`/`route`, response/revision `done`) are suppressed under `--json` so
    stdout is exactly one JSON document.
  - `src/turma/planning/...` (`PlanningSession`) — a `quiet` flag / output sink
    threaded to every progress-print site (the nodes already receive the
    session), so suppression covers the fresh-plan loop *and* `--resume --revise`
    (which re-runs the loop).
  - `src/turma/cli.py` — `plan` subparser gains `--json`; the plan + resume
    branches select the JSON snapshot renderer and route `PlanningError` through
    the JSON error object in `--json` mode.
  - `tests/test_planning.py` / `tests/test_planning_resume.py` /
    `tests/test_swarm_cli.py` (or the planning CLI test module) — new `--json`
    tests pinning the snapshot for each state + the resume `action` + the error
    object; existing text tests stay **unchanged** (the byte-for-byte guard).
  - `README.md`, `docs/architecture.md`, `CHANGELOG.md`.
- **No new runtime deps.**

## Out of Scope

- **NDJSON / per-round streaming of planning progress.** `plan` reports one
  terminal outcome; streaming author/critic rounds mid-loop is a separate change
  that would require the state machine to emit progress.
- **`turma plan-to-beads --json`.** A separate command with its own output
  (`feature` / `marker` / `tasks`); a natural follow-on, not this change.
- **MCP / TUI / VS Code / web surfaces.** Deferred pending consolidation.
- **Any change to the planning loop, state machine, gate semantics, or text
  output.**
