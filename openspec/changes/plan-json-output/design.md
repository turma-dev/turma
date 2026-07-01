## Scope

Add a `turma.plan.v1` JSON snapshot to `turma plan` (fresh and resume). This is
the snapshot analog of `turma status --json` — a single object describing the
terminal `PlanningGraphResult`, not a stream. It completes the operator API's
read side; it does not touch the planning loop, state machine, or gate.

## Why snapshot, not NDJSON

`turma run` is a live per-task loop, so it streams events. `turma plan` is not:
each invocation drives the LangGraph state machine to exactly one
suspended-or-terminal outcome (`run_planning_state_machine` /
`resume_plan` return one `PlanningGraphResult`) and reports it. The natural
machine shape is therefore one JSON object per invocation — a snapshot. Streaming
per author/critic round would require the state machine to emit mid-loop
progress; that is a separate change (see proposal Out of Scope).

## The snapshot

```json
{
  "schema": "turma.plan.v1",
  "feature": "<name>",
  "state": "awaiting_human_approval",
  "round": 2,
  "next_nodes": ["human_approval"],
  "checkpoint": "<checkpoint_path>",
  "artifacts_dir": "openspec/changes/<name>/",
  "action": null
}
```

Every field derives from data the command already has:

- `state` — `result.state.get("state")`. One of `awaiting_human_approval`,
  `approved`, `needs_revision`, `abandoned`, `needs_human_review` (the committed
  planning vocabulary).
- `round` — `int(result.state.get("round", 1))`.
- `next_nodes` — `list(result.next_nodes)`; `[]` when the plan reached a terminal
  state (not suspended).
- `checkpoint` — `str(result.checkpoint_path)`.
- `artifacts_dir` — `"openspec/changes/<feature>/"` (the same path the text hints
  reference).
- `action` — for resume invocations, the `ResumeAction` value
  (`status`/`approve`/`revise`/`abandon`/`override_approve`); `null` for a fresh
  `plan`.

One object, printed once, to stdout. `json.dumps(..., indent=2)` for readability
(consistent with `status --json`; a snapshot, so indentation is fine — unlike
`run`'s NDJSON which must be one line per event).

## Rendering seam — ALL planning stdout must be suppressed in JSON mode

`plan` is not just a terminal renderer. The author/critic loop prints **progress
throughout `run_planning_state_machine`**, deep inside the graph nodes, not only
in `run_planning`'s outcome block. In `src/turma/planning/__init__.py` today:

- preamble (`run_planning`): `loading config` / `author model` / `creating
  change` (112-114).
- **drafting node:** `generating <artifact> (this may take 1-2 min) ...` + `done`
  (249, 276).
- **critic node:** `critic status: <...>` / `critic parse failure: <...>` /
  `critic route: <...>` (314, 316, 317).
- **response / revision nodes:** progress + `done` (403, 423, 493, 526).
- outcome + resume-command hints (`run_planning` / `_print_resume_command_hints`,
  120-157).

If `--json` only re-rendered `run_planning`'s outcome block, a fresh plan or a
`--resume --revise` (which re-enters the loop) would interleave these progress
lines with the JSON object, and `json.loads(stdout)` would fail. **The contract
is: in `--json` mode, stdout contains exactly one JSON document — nothing else.**

Mechanism: the graph nodes all receive the `PlanningSession`, so thread output
suppression through it — a `quiet: bool` (or an output sink) on `PlanningSession`
that every progress `print(...)` site respects. `run_planning` / `resume_plan`
construct the session with `quiet=True` under `--json`. Then:

- **text mode** (`quiet=False`) — every line above prints exactly as today
  (preamble, per-node progress, outcome, resume hints). Byte-for-byte unchanged;
  the existing text tests are the guard.
- **json mode** (`quiet=True`) — all progress and outcome text is suppressed;
  the CLI prints a single `_plan_snapshot(result, feature, action)` dict via
  `json.dumps(indent=2)`, and nothing else reaches stdout.

The CLI `plan` branch chooses text vs json for both the fresh-plan path
(`run_planning`) and the resume path (`resume_plan` + `_print_resume_result`,
which becomes snapshot-aware) — and `--resume --revise`, which re-runs loop
nodes, is covered because suppression lives on the session the nodes use.

## Error shape

On `PlanningError` in `--json` mode, emit a single
`{"schema": "turma.plan.v1", "error": "<message>"}` object and exit nonzero, so a
consumer always parses JSON. This is deliberately *more* consumer-friendly than
`status --json`, which currently still prints `error: <msg>` text on failure
(status raises before rendering). The trade-off: plan diverges slightly from the
status precedent for the benefit of uniform JSON. If the reviewer prefers strict
consistency with status, the alternative is to keep the shared `error: <msg>`
text path even under `--json`; the recommendation here is the structured error.

## Text output is byte-for-byte unchanged

`--json` only selects a different renderer. The preamble (`loading config`,
`author model`, `creating change`), the outcome lines, and the resume-command
hints are all unchanged when the flag is absent — pinned by the existing
planning CLI text tests, which must pass with no edits.

## Tests

Test-first, dual pin:

- **Text unchanged.** Existing `plan` / resume text assertions pass with no
  edits (the byte-for-byte guard for the render factor-out) — this also proves
  per-node progress still prints in text mode.
- **stdout is exactly one JSON document.** For a fresh plan *and* for a
  `--resume --revise` (both of which run loop nodes that print progress),
  `--json` stdout must parse as a single object with no leftover progress lines:
  `obj = json.loads(captured.out)` succeeds and `captured.out.strip()` contains
  exactly that one document (e.g. no `generating …` / `critic route:` / `done`
  text). This is the regression guard for the suppression mechanism.
- **New `--json` snapshots**, one per outcome, parsed with `json.loads`:
  - fresh plan suspended at the gate → `state: awaiting_human_approval`,
    `round`, `next_nodes` non-empty, `action: null`.
  - `needs_human_review`, `abandoned`, and `approved` (complete) terminal states.
  - resume `--approve` / `--revise` / `--abandon` / `--override` → the
    post-transition `state` and the matching `action`.
  - resume read-only (`--resume --json`, no action) → `action: "status"` and the
    current state.
  - every snapshot has `schema == "turma.plan.v1"`.
- **Error object.** A `PlanningError` under `--json` yields
  `{"schema": "turma.plan.v1", "error": ...}` and exit 1; text mode still yields
  `error: <msg>`.

## Out of items deferred past this change

- NDJSON / per-round planning progress streaming.
- `turma plan-to-beads --json`.
- Aligning `status --json`'s error path to the structured error shape (a
  separate, optional consistency pass).
- MCP / TUI / VS Code / web surfaces.
