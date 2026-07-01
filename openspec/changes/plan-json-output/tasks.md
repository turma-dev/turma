## Tasks

### 1. Pin the snapshot with failing tests first

Coverage note: the snapshot fields come from one shared `plan_snapshot`
builder, so a unit test pins the field mapping for any `state`/`action`; the
CLI/`main(...)` tests then pin the representative paths end to end rather than
one test per state/action.

- [x] fresh plan suspended at gate → `schema == "turma.plan.v1"`,
      `state == "awaiting_human_approval"`, non-empty `next_nodes`, `action is
      None`, `artifacts_dir == "openspec/changes/<f>/"`
      (`test_run_planning_json_is_a_single_snapshot_document`, via
      `run_planning(as_json=True)`).
- [x] **stdout is exactly one JSON document** for a fresh plan AND a
      `--resume --revise` (both run loop nodes that print progress): parses to
      one object, no `generating …` / `critic route:` / `done` / preamble leaked
      (fresh: `test_run_planning_json_…`; revise via `main`:
      `test_plan_resume_json_via_cli_revise_and_readonly`).
- [x] resume via `main(...)`: read-only (`--resume --json` → `action ==
      "status"`) and `--revise --json` (→ `action == "revise"`, `round == 2`),
      single document (`test_plan_resume_json_via_cli_revise_and_readonly`).
- [x] `plan_snapshot` field mapping (unit): `state`/`round`/empty `next_nodes`/
      `artifacts_dir`/`action` for a terminal `approved` + `action="approve"`
      (`test_plan_snapshot_carries_state_round_and_resume_action`).
- [x] error object: a `PlanningError` under `--json` →
      `{"schema": "turma.plan.v1", "error": "<msg>"}` and exit 1
      (`test_plan_json_error_is_a_single_json_object`).
- [~] Not individually CLI-pinned (covered structurally by the shared
      `plan_snapshot` + the state-machine's own resume/terminal tests):
      `--approve` / `--abandon` / `--override` CLI actions and the
      `needs_human_review` / `abandoned` terminal snapshots. Add per-action CLI
      tests if a consumer depends on those exact end-to-end paths.
- [x] Confirmed the new tests fail for the right reason before implementing.

### 2. Suppress ALL planning progress output in JSON mode

- [x] Thread output suppression through `PlanningSession` (a `quiet: bool` or an
      output sink) so **every** progress `print(...)` in the loop nodes respects
      it: drafting (`generating …` / `done`), critic (`status` / `parse
      failure` / `route`), response/revision (`done`). The nodes already receive
      the session.
- [x] `run_planning` / `resume_plan` construct the session with `quiet=True`
      under `--json`; the preamble + outcome + resume-hint prints in
      `run_planning` are likewise gated so nothing but the snapshot reaches
      stdout.
- [x] Text mode (`quiet=False`) prints every line exactly as today (byte-for-byte).

### 3. Factor the plan renderer + add the JSON snapshot

- [x] Factor `run_planning` so the terminal `PlanningGraphResult` is available to
      a renderer rather than printed inline; keep a `_render_plan_text(...)` that
      reproduces today's exact lines (preamble + outcome + resume hints).
- [x] Add `_plan_snapshot(result, feature, action) -> dict` and a JSON renderer
      (`json.dumps(indent=2)`, top-level `schema = "turma.plan.v1"`).

### 4. CLI wiring

- [x] `plan` subparser in `cli.py` gains `--json` (`action="store_true"`).
- [x] The fresh-plan and resume branches select text vs JSON; `_print_resume_result`
      becomes snapshot-aware (carries the `action`).
- [x] In `--json` mode, `PlanningError` → the JSON error object + exit 1; text
      mode unchanged (`error: <msg>`).

### 5. Green + baseline

- [x] New `--json` tests pass; existing plan/resume **text tests pass with no
      edits** (byte-for-byte guard).
- [x] Full baseline: `uv sync`, `uv run turma init`, `uv run turma --help`,
      `uv run python -m turma --help`, `uv run pytest`.

### 6. Docs + changelog

- [x] `README.md`: document `turma plan --json` and the `turma.plan.v1` snapshot
      (states, `action`, error object).
- [x] `docs/architecture.md`: note the plan snapshot completing the operator API
      surface (status snapshot / run stream / plan snapshot).
- [x] `CHANGELOG.md` `[Unreleased]`.

### 7. Scope guard

- [x] Diff limited to: `planning/__init__.py` (progress suppression + render
      factor-out + snapshot), `PlanningSession` (`quiet`/sink), `cli.py`
      (`--json` flag + wiring), the planning CLI test module,
      README/architecture/changelog. No state-machine/gate change, no text-output
      change, no NDJSON, no `plan-to-beads --json`.
