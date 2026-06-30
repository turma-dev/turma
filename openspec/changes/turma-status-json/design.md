## Scope

Add a `--json` rendering to `turma status`. One module (`status.py`) grows a
second renderer; the CLI grows one flag. No new adapter reads, no change to the
text output, no state-machine impact.

## Gather / render split

`status_readout` today does gather-then-render inline. Factor the gather half
into a typed model so both renderers consume it:

```
status_readout(feature, *, services, repo_root, as_json=False) -> str
  model = _gather(feature, services=services, repo_root=repo_root)   # all adapter reads
  return _render_json(model) if as_json else _render_text(model)     # pure formatting
```

- `_gather` runs the exact adapter pipeline that lives in `status_readout`
  today, in the same order (all-statuses snapshots → ready → in-progress +
  retries → branches → PRs → per-in-progress PR state gated on the
  `turma-pr:<N>` label). It performs only the existing read-only calls.
- `_render_text(model)` is today's `_render` unchanged — its output must remain
  byte-for-byte identical, which the existing text tests already pin.
- `_render_json(model)` is the new pure function: model → `json.dumps(...,
  indent=2)`.

The model is the structured data already computed today (`_Preflight`,
`_Buckets`, the task tuples, `in_progress_retries`, `in_progress_pr_states`,
`branches`, `prs`) plus, for each in-progress task, its worktree presence/path
and structured sentinel — both of which the text renderer already derives via
`worktree_path_for` + `_describe_sentinel`. Whether this is a new `_StatusModel`
dataclass or the existing kwargs bundle passed to both renderers is an
implementation detail; the contract is that gather is shared and rendering is
pure.

## JSON contract

Top-level keys, in this order:

- `schema` — literal `"turma.status.v1"`. Stable identifier; not a versioning
  system (see proposal Out of Scope).
- `feature` — the feature name.
- `spec` — `{ change_dir, present, approved, transcribed }` from `_Preflight`.
  `change_dir` is the `openspec/changes/<feature>/` string; `present` is
  `change_dir_exists`.
- `tasks` — the `_Buckets` counters:
  `{ ready, in_progress, blocked_deferred, closed, needs_human_review }`.
- `ready` — list of `{ id, title }` in the gathered order.
- `in_progress` — list of objects (see below).
- `pull_requests` — list of `{ number, url, state, title, head_branch }` from
  `PrSummary`, in gathered order.
- `orphan_branches` — list of branch-name strings (the same filtered set the
  text renderer computes: feature branches with no in_progress task).

### `in_progress[]` object

```
{
  "id": str,
  "title": str,
  "retries": int,                 # retries.get(id, 0)
  "max_retries": int,             # services.max_retries
  "worktree": { "present": bool, "path": str | null },
  "sentinel": { "status": "complete" | "failed" | null, "reason": str | null },
  "pr": { "number": int, "state": str, "url": str } | null
}
```

- `worktree.present` is `worktree_path_for(...).is_dir()`. `worktree.path` is
  the absolute path string when present, `null` when absent. This mirrors the
  text line `worktree: <path>/ (present|absent)`.
- `sentinel` restructures `_describe_sentinel`:
  - complete → `{ "status": "complete", "reason": null }`
  - failed → `{ "status": "failed", "reason": "<first line of .task_failed>" }`
    (same first-line truncation the text path uses; unreadable file →
    `reason: "<could not read sentinel>"`, matching the text fallback)
  - none / worktree absent → `{ "status": null, "reason": null }`
- `pr` is populated only when the task has a `turma-pr:<N>` label (the gather
  step's `in_progress_pr_states` already encodes exactly this gating);
  otherwise `null`. No extra `gh` call is introduced — JSON reads the same map
  the text path reads.

## Determinism

`json.dumps(payload, indent=2)` with the dict assembled in the key order above
and lists in gathered order. No `sort_keys` (insertion order is the contract).
Only data-derived values appear — no timestamps, paths-of-the-moment beyond the
worktree path the text already emits, or other run-varying content. This makes
the output snapshot-pinnable.

## Errors

`_gather` propagates `PlanningError` from any failing adapter read, exactly as
`status_readout` does today. The renderer is only reached on a fully-gathered
model, so a partial JSON document is never emitted. Missing spec dir / APPROVED
/ TRANSCRIBED render as `present/approved/transcribed: false` — "no state yet"
is valid state, not an error, consistent with the text path.

## CLI

`status` subparser gains `--json` (`action="store_true"`). The `status` branch
in `cli.py` calls `status_readout(..., as_json=args.json)` and prints the
result. Exit code and the `ConfigError`/`PlanningError` → `error: <msg>` → exit
1 handling are unchanged.

## No-mutation invariant

The headline `turma-status` guarantee — zero calls to any mutating adapter
surface — must hold in `--json` mode too, because gather is the same pipeline.
Tests assert it explicitly for the JSON path (not just inherited from the text
tests).

## Tests

`tests/test_swarm_status.py`, test-first:

1. **Text-unchanged guard:** existing text tests must still pass untouched —
   `_render_text` is byte-for-byte today's `_render`.
2. **JSON shape:** drive a populated feature; parse `--json` output with
   `json.loads`; assert `schema == "turma.status.v1"`, the `spec`/`tasks`
   blocks, and one fully-populated `in_progress[]` entry (worktree present+path,
   `sentinel.status == "failed"` with reason, `pr` populated).
3. **Nullability:** a task with no worktree → `worktree.present false`,
   `path null`, `sentinel {null,null}`; a task with no `turma-pr:` label →
   `pr null`; a complete sentinel → `{ "complete", null }`.
4. **No-mutation in JSON mode:** assert zero mutating adapter calls when
   `as_json=True` (mirror the existing text headline test).
5. **Determinism:** the JSON string is stable across two calls on the same
   gathered state (no ordering churn).
6. **CLI wiring:** `--json` reaches `status_readout(as_json=True)`; default
   (no flag) still renders text.

## Out of items deferred past this change

- `--json` for `plan` / `run`.
- Schema-version negotiation / multiple versions.
- Additive fields (timestamps, durations, SHAs) — need a concrete consumer.

Listed so a future machine-output change has a clean boundary.
