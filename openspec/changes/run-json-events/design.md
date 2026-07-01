## Scope

Add an NDJSON `--json` event stream to `turma run`, via an emitter seam that
replaces the orchestrator's scattered `print(...)` calls. The design challenge
is not the JSON — it is that `run` has **no single render seam** (unlike
`status`'s gather→render): output is interleaved with orchestration logic at
~18 sites. The emitter is that seam.

## The emitter seam

```python
class RunEmitter(Protocol):
    def emit(self, event: str, /, **fields) -> None: ...
```

- **`TextEmitter`** — for each event, writes today's exact line(s) to stdout.
  It owns the format strings currently inline in the `print(...)` calls, so the
  text output is reproduced byte-for-byte. The existing `capsys` assertions in
  `tests/test_swarm_run.py` are the regression guard and must not change.
- **`JsonEmitter`** — writes `json.dumps({"schema": SCHEMA, "event": event,
  **fields})` + `"\n"` to stdout, then `flush()`. `SCHEMA = "turma.run.v1"`.
  Compact (no `indent`) — one object per line is the NDJSON contract. Flushing
  per event is required so streaming consumers see progress live.

**Placement (no global).** The emitter is a field on `SwarmServices` (a plain
`@dataclass`, the DI boundary), defaulting to `TextEmitter()` so existing
construction — including the test stubs in `_make_services` — keeps working
unchanged. The field is added after the required fields (it has a default).

**Reconciliation wrinkle.** `reconcile_feature` (in `reconciliation.py`) does
its own `reconcile:` prints but is called with adapters, not `services`. It
gains an `emitter: RunEmitter` parameter (default `TextEmitter()`), and
`run_swarm` passes `services.emitter` when it calls it. This is the one signature
change outside `SwarmServices`.

Each `print(...)` site becomes `emitter.emit("<event>", **fields)`. Multi-line
prints map to a single event whose `TextEmitter` rendering spans the same lines.

## Event catalog (1:1 with today's text transitions)

Envelope on every event: `{"schema": "turma.run.v1", "event": <name>, ...}`.
The set below is exactly today's output lines — this change re-encodes, it does
not add telemetry.

| event | fields | text line it replaces |
|---|---|---|
| `fetch_skipped` | — | `fetch: skipped (--dry-run)` |
| `fetch_advanced` | `base_branch` | `fetch: origin/<b> → <b>` |
| `reconcile_summary` | `in_progress_count` | `reconcile: N in-progress …` |
| `reconcile_finding` | `task_id?`, `kind`, `detail` | `reconcile:   <finding>` |
| `repair` | `task_id`, `action`, `reason?` | `repair: <id> → …` |
| `repair_orphan_branch` | `branch` | `repair: orphan branch …` |
| `merge_advancement` | `task_id`, `pr_number`, `pr_state`, `action`, `dry_run` | `merge-advancement:` / `would:` (all 5 variants — see below) |
| `stopping_max_tasks` | `max_tasks` | `swarm: stopping at --max-tasks=N` |
| `done` | `reason` (`"no_ready_tasks"`) | `swarm: no ready tasks remain; done` |
| `claim_race` | `task_id`, `detail` | `swarm: claim race on <id> …` |
| `task_claimed` | `task_id`, `title` | `swarm: claimed <id> — <title>` |
| `worktree_setup` | `task_id` | `worktree: setup <id>` |
| `worker_running` | `task_id`, `timeout_s` | `worker: running <id> (timeout Ns)` |
| `commit` | `task_id` | `commit: <id>` |
| `push` | `task_id` | `push: <id>` |
| `task_opened` | `task_id`, `pr_number`, `pr_url` | `swarm: opened <id> (PR: … )` |
| `task_failed` | `task_id`, `attempt`, `max_attempts`, `reason`, `budget_exhausted` | `swarm: <id> failed (…)` |
| `bd_state_unpropagated` | — | bd-state tail-mutation warning |
| `error` | `message` | CLI `error: <msg>` (terminal, `--json` only) |

The exact field names/values are pinned by the tests (Task 1).

**Merge-advancement has five distinct text lines today**
(`_advance_merged_prs`), and the 1:1 guardrail means all five map to a
`merge_advancement` event, distinguished by `pr_state` + `action` (each pinned
by a test — none may be dropped or under-specified):

| `pr_state` | `action` | `dry_run` prefix? | today's line |
|---|---|---|---|
| `MERGED` | `closed` | yes (`would:`) | `… → MERGED, closed` |
| `CLOSED` | `failed` | yes (`would:`) | `… → CLOSED without merge → fail_task` |
| `OPEN` | `left_alone` | **no** (read-only either way) | `… → OPEN, leaving alone` |
| `<other>` | `left_alone_unrecognized` | **no** | `… → unrecognized state '<state>', leaving alone` |
| `not_found` | `halting_stale` | **no** | `… → 404; halting (turma-pr:<N> stale; triage)` |

The **404 / `halting_stale`** variant is special: it emits the
`merge_advancement` event and then **raises** (stale `turma-pr:` label). In
`--json` mode that raise becomes the terminal `error` event (see Failure
behavior). It must not be dropped — it is an existing operator-visible
transition *and* a halt. `dry_run` is a field on every variant, but only MERGED
/ CLOSED actually change behavior under dry-run; OPEN / unrecognized / 404 are
read-only and carry no `would:` prefix in the text today, so their `TextEmitter`
rendering omits the prefix regardless.

## Failure behavior

- Errors still **exit nonzero** — unchanged.
- Events already emitted before a failure are complete, flushed NDJSON records;
  a consumer that read them has valid data. NDJSON has no document-level wrapper
  to leave half-written, which is exactly why it survives mid-run failure.
- `task_failed` covers the orchestrator's `swarm: <id> failed …` line
  (retry/timeout/clean-tree/exhaustion).
- The CLI's terminal `error: <msg>` (its shared `PlanningError`/`ConfigError`
  handler) becomes an `error` event **in `run --json` mode only** — the run
  branch of `cli.py` emits `{"schema":"turma.run.v1","event":"error","message":…}`
  as the last NDJSON line, then exits 1. Other commands' error text is untouched.
  This is the only CLI-layer emit; everything else flows through the
  orchestrator emitter.

## Text output is byte-for-byte unchanged

The `TextEmitter` reproduces the current lines exactly; `--json` only swaps the
emitter. This is the same discipline as `status --json` (text renderer
untouched). The proof is that every existing `capsys` assertion in
`tests/test_swarm_run.py` passes without edits.

## Tests

Dual pinning, test-first:

- **Text unchanged (regression guard).** The existing `capsys` text assertions
  in `tests/test_swarm_run.py` must pass with **no edits** after the emitter
  refactor. This is the primary safety net for the byte-for-byte guarantee.
- **New `--json` tests.** For representative paths — happy single-task loop,
  worker-failure/retry, reconcile+repair, merge-advancement, dry-run,
  no-ready-tasks, max-tasks stop — drive `run_swarm` with a `JsonEmitter`
  capturing to a buffer (or `capsys`), parse each line with `json.loads`, and
  assert: every line has `schema == "turma.run.v1"`; the **event order** matches
  the run's phases; and the **payload** of each event carries the expected
  fields (e.g. `task_opened` has `pr_number`/`pr_url`; `worker_running` has
  `timeout_s`; `task_failed` has `attempt`/`max_attempts`/`budget_exhausted`).
- **Failure path.** A run that halts on retry-exhaustion emits the `task_failed`
  event(s) and then the terminal `error` event, each a valid NDJSON line, and
  the process exits nonzero.
- **CLI wiring.** `--json` reaches the emitter selection; default (no flag)
  uses `TextEmitter`.

## Out of items deferred past this change

- `plan` JSON (state/gate transitions — its own shape and change).
- Worker stdout/stderr streaming.
- Schema versioning beyond the `schema` string.
- Any richer telemetry (timings, durations, token counts) — new events for new
  data, later, with a consumer.
