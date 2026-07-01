## Tasks

### 1. Pin the snapshot with failing tests first

- [ ] Add `--json` tests (in the planning CLI test module) that drive
      `turma plan` / resume via `main(...)` and `json.loads` the stdout:
  - [ ] fresh plan suspended at gate → `schema == "turma.plan.v1"`,
        `state == "awaiting_human_approval"`, `round`, non-empty `next_nodes`,
        `action is None`, `artifacts_dir == "openspec/changes/<f>/"`.
  - [ ] terminal states: `needs_human_review`, `abandoned`, `approved`
        (complete) → correct `state`, empty `next_nodes`.
  - [ ] resume `--approve` / `--revise "<r>"` / `--abandon "<r>"` /
        `--approve --override "<r>"` → post-transition `state` + matching
        `action` (`approve`/`revise`/`abandon`/`override_approve`).
  - [ ] resume read-only (`--resume --json`, no action) → `action == "status"`
        + current state.
  - [ ] error object: a `PlanningError` under `--json` →
        `{"schema": "turma.plan.v1", "error": "<msg>"}` and exit 1.
  - [ ] **stdout is exactly one JSON document** for a fresh plan AND a
        `--resume --revise` (both run loop nodes that print progress):
        `json.loads(captured.out)` succeeds and `captured.out.strip()` is that
        one document only — no `generating …` / `critic route:` / `done` /
        preamble text leaked in.
- [ ] Confirm these fail for the right reason (no `--json` yet).

### 2. Suppress ALL planning progress output in JSON mode

- [ ] Thread output suppression through `PlanningSession` (a `quiet: bool` or an
      output sink) so **every** progress `print(...)` in the loop nodes respects
      it: drafting (`generating …` / `done`), critic (`status` / `parse
      failure` / `route`), response/revision (`done`). The nodes already receive
      the session.
- [ ] `run_planning` / `resume_plan` construct the session with `quiet=True`
      under `--json`; the preamble + outcome + resume-hint prints in
      `run_planning` are likewise gated so nothing but the snapshot reaches
      stdout.
- [ ] Text mode (`quiet=False`) prints every line exactly as today (byte-for-byte).

### 3. Factor the plan renderer + add the JSON snapshot

- [ ] Factor `run_planning` so the terminal `PlanningGraphResult` is available to
      a renderer rather than printed inline; keep a `_render_plan_text(...)` that
      reproduces today's exact lines (preamble + outcome + resume hints).
- [ ] Add `_plan_snapshot(result, feature, action) -> dict` and a JSON renderer
      (`json.dumps(indent=2)`, top-level `schema = "turma.plan.v1"`).

### 4. CLI wiring

- [ ] `plan` subparser in `cli.py` gains `--json` (`action="store_true"`).
- [ ] The fresh-plan and resume branches select text vs JSON; `_print_resume_result`
      becomes snapshot-aware (carries the `action`).
- [ ] In `--json` mode, `PlanningError` → the JSON error object + exit 1; text
      mode unchanged (`error: <msg>`).

### 5. Green + baseline

- [ ] New `--json` tests pass; existing plan/resume **text tests pass with no
      edits** (byte-for-byte guard).
- [ ] Full baseline: `uv sync`, `uv run turma init`, `uv run turma --help`,
      `uv run python -m turma --help`, `uv run pytest`.

### 6. Docs + changelog

- [ ] `README.md`: document `turma plan --json` and the `turma.plan.v1` snapshot
      (states, `action`, error object).
- [ ] `docs/architecture.md`: note the plan snapshot completing the operator API
      surface (status snapshot / run stream / plan snapshot).
- [ ] `CHANGELOG.md` `[Unreleased]`.

### 7. Scope guard

- [ ] Diff limited to: `planning/__init__.py` (progress suppression + render
      factor-out + snapshot), `PlanningSession` (`quiet`/sink), `cli.py`
      (`--json` flag + wiring), the planning CLI test module,
      README/architecture/changelog. No state-machine/gate change, no text-output
      change, no NDJSON, no `plan-to-beads --json`.
