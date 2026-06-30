## Tasks

### 1. Pin the JSON contract with failing tests first

- [ ] In `tests/test_swarm_status.py`, add JSON-mode tests that drive
      `status_readout(..., as_json=True)` (or the chosen entry point) and
      `json.loads` the result:
  - [ ] **shape** — `schema == "turma.status.v1"`; `feature`; `spec`
        `{change_dir, present, approved, transcribed}`; `tasks` counters;
        `ready` / `pull_requests` / `orphan_branches` lists in gathered order.
  - [ ] **populated in_progress entry** — `worktree {present: true, path}`,
        `sentinel {status: "failed", reason: "<first line>"}`, `pr {number,
        state, url}`.
  - [ ] **nullability** — no worktree → `worktree.present false` + `path null`
        + `sentinel {null, null}`; no `turma-pr:` label → `pr null`; complete
        sentinel → `{status: "complete", reason: null}`.
  - [ ] **determinism** — two calls on the same stubbed state produce identical
        JSON strings.
- [ ] Add a **no-mutation in JSON mode** test mirroring the existing text
      headline test: zero calls to every mutating adapter surface when
      `as_json=True`.
- [ ] Confirm these fail for the right reason (no JSON path yet) before
      touching `status.py`.

### 2. Factor gather / render in `status.py`

- [ ] Extract the adapter read pipeline currently inline in `status_readout`
      into a `_gather(...)` step returning a structured model (new
      `_StatusModel` dataclass or a shared kwargs bundle — implementation
      choice), preserving the exact read order and read-only calls.
- [ ] Keep today's `_render` as `_render_text(model)` with **byte-for-byte
      identical** output (the existing text tests are the guard).
- [ ] Add `_render_json(model) -> str` building the payload dict in the
      contract's key order and `json.dumps(payload, indent=2)` (no
      `sort_keys`).
- [ ] Structure the sentinel as `{status, reason}` from the same source as
      `_describe_sentinel` (reuse its first-line / unreadable-file handling).
- [ ] Add `worktree {present, path}` from `worktree_path_for(...).is_dir()` and
      the absolute path (null when absent).
- [ ] `status_readout` gains `as_json: bool = False` and dispatches to the
      right renderer over one gathered model.

### 3. CLI wiring

- [ ] `status` subparser in `src/turma/cli.py` gains `--json`
      (`action="store_true"`).
- [ ] The `status` branch passes `as_json=args.json` into `status_readout` and
      prints the result; `ConfigError`/`PlanningError` → `error:` → exit 1
      handling unchanged.

### 4. Green + guard

- [ ] Run `tests/test_swarm_status.py`: new JSON tests pass; every existing
      text test still passes unchanged.
- [ ] Full validation baseline: `uv sync`, `uv run turma init`,
      `uv run turma --help`, `uv run python -m turma --help`, `uv run pytest`.

### 5. Docs + changelog

- [ ] `README.md` Feature Status section: document `turma status --feature
      <name> --json` and the `turma.status.v1` payload.
- [ ] `docs/architecture.md`: one line noting the machine-readable status mode
      (no state-machine change).
- [ ] `CHANGELOG.md` `[Unreleased]`: additive `--json` for `turma status`.

### 6. Scope guard

- [ ] Confirm the diff is limited to: `status.py` (gather/render split + JSON
      renderer), `cli.py` (one flag + passthrough), `test_swarm_status.py`,
      README + architecture + changelog. No adapter changes, no new `bd`/`gh`
      calls, no text-output change, no `plan`/`run` `--json`.
