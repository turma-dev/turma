## Scope

A single sweep at the top of `turma run` that observes GitHub
PR state for the feature's outstanding task PRs and advances
Beads accordingly. The success path of `_run_single_task` stops
at `open_pr` instead of closing the Beads task; the close moves
to the sweep, which only fires after GitHub reports the PR as
merged.

Single-feature, sequential, one sweep per `turma run`
invocation. No background polling, no global scheduler, no
parallel claims.

## The six gating questions

The change set is small in code volume but shifts a behavior
contract. These are the questions the spec must answer
unambiguously before any code lands. Each is pinned with the
specific name / value the implementation will use.

### 1. What exact source of truth decides "merged"?

GitHub PR state, queried via `gh pr view <N> --json state`.
`state == "MERGED"` is the canonical merged signal. The
orchestrator records the PR number on the Beads task at
`open_pr` time via a `turma-pr:<N>` label; the
merge-advancement sweep reads the label, looks up that exact
PR by number, and dispatches on `state`.

`gh`'s state vocabulary for a PR is fixed:

| state | Meaning | Advancement action |
| --- | --- | --- |
| `OPEN` | Not yet merged or closed | Leave alone |
| `DRAFT` | Open as draft | Leave alone (treated identically to OPEN) |
| `MERGED` | Merged | `unmark_pr_open` → `close_task` → `cleanup_worktree` |
| `CLOSED` | Closed without merge | `unmark_pr_open` → `fail_task("PR #<N> closed without merge")` |

Other JSON fields (`mergedAt`, `mergeCommit`) are ignored —
`state == "MERGED"` is sufficient. Capturing `mergedAt` would
be useful for telemetry, but v1 is read-and-react, not
report-and-store.

### 2. When in the run loop do we check?

Once per `turma run` invocation, in a new phase
`_advance_merged_prs` between `_apply_repairs` and
`_main_loop`:

```
preflight
    └─▶ reconcile (read-only)
            └─▶ repair_phase
                    └─▶ merge_advancement_phase   ← NEW
                            └─▶ fetch_ready
                                    └─▶ … main loop
```

No mid-loop re-checks. The "world at run-start" is the model
the orchestrator operates under; if the operator wants to pick
up a merge that landed mid-run, they re-run `turma run`. This
matches reconciliation's same-shaped discipline and keeps the
sweep cost predictable (one `gh` batch query per invocation,
not per-iteration).

`--dry-run` runs the sweep in **read-only** mode: it queries
PR state but applies no Beads or worktree mutations, just like
reconciliation. Operators get a preview of what advancement
would do without committing.

### 3. What Beads mutation happens on merge?

Two-step, both inside the merge-advancement handler for the
MERGED case:

1. `BeadsAdapter.unmark_pr_open(task_id, pr_number)` →
   `bd update <task_id> --remove-label turma-pr:<N>`. Prevents
   stale labels surviving on a closed task and confusing the
   next sweep.
2. `BeadsAdapter.close_task(task_id)` → `bd close <task_id>`
   (existing). Triggers bd's dep advancement: any task
   blocked-by this one becomes a candidate for `bd ready` on
   the next `fetch_ready`.

After both, `WorktreeManager.cleanup(ref)` removes the
per-task worktree + branch — the work is integrated, the
on-disk artifacts are no longer triage candidates.

The current happy-path tail (`open_pr` → `close_task` →
`cleanup_worktree` inside `_run_single_task`) ends at
`open_pr` + `mark_pr_open`. The close + cleanup migrate to
the merge-advancement phase, but the **adapter calls
themselves are unchanged** — the sweep just relocates them.
This minimizes blast radius on existing tests for those
adapter methods.

### 4. What happens if the PR is closed without merge?

`gh pr view <N>` returns `state == "CLOSED"` and
`mergedAt == null`. Treated as a worker failure with a fixed
reason:

1. `BeadsAdapter.unmark_pr_open(task_id, pr_number)` (clean
   the label first so retry attempts don't carry a stale
   pr-open marker).
2. `_handle_failure(services, task_id, f"PR #{N} closed
   without merge")` — same retry-budget machinery the main
   loop uses. Budget remaining → task returns to `open` and
   becomes ready again on a future run; budget exhausted →
   `needs_human_review` label, halt the run.

The worktree **stays on disk** per the v1 Worktree contract
("failed worktrees are never auto-removed; primary triage
artifact"). The branch + closed PR + worktree triple gives
the operator everything needed to inspect why review
rejected the work.

### 5. What happens if GitHub and Beads disagree?

Five disagreement classes, each with a pinned response. The
sweep is the only place these are detected; reconciliation
already covers the broader "interrupted state" cases, so the
merge-advancement handler only owns the label-driven cases.

| Beads state | label | gh result | Response |
| --- | --- | --- | --- |
| in_progress | `turma-pr:<N>` | `state == OPEN` / `DRAFT` | leave alone |
| in_progress | `turma-pr:<N>` | `state == MERGED` | unmark + close + cleanup |
| in_progress | `turma-pr:<N>` | `state == CLOSED` (mergedAt null) | unmark + fail_task |
| in_progress | `turma-pr:<N>` | `gh` returns "PR not found" / 404 | `PlanningError` — operator triage |
| closed | `turma-pr:<N>` lingering | any state | log only; do not mutate |

Last row covers the "we crashed between unmark and close" or
"operator manually closed the bd task while the label was
still set" cases. The sweep prints a `merge-advancement:
<task_id> closed task carries stale turma-pr:<N> label`
warning and continues — automatic cleanup would be a
mutation the operator didn't request.

The `gh returns 404` row is the only path that raises. That
case (label says PR <N> exists, gh says no PR <N>) means
either the PR was deleted (rare; GitHub typically only
allows that for spam) or the recorded number was wrong. Both
need operator decision; the sweep refuses to guess.

The "in_progress task with no `turma-pr:<N>` label" case is
explicitly **not** the merge-advancement handler's job —
that's reconciliation's `completion-pending` /
`completion-pending-with-pr` / `stale-no-sentinels`
territory, already handled by `_apply_repairs` upstream.

### 6. Does one invocation advance one merged task or sweep all?

**Sweep all.** A single batched query plus per-task dispatch:

1. `services.beads.list_in_progress_tasks(feature)` (already
   exists from the swarm-orchestration arc).
2. Filter to tasks whose labels contain a `turma-pr:<N>`
   marker; parse `N` from each.
3. For each `(task, N)`, call
   `services.pr.get_pr_state_by_number(N)` and dispatch.
4. Handlers fire serially in the order tasks were returned.
   Errors from any single task's handler propagate (the
   sweep does not silently skip on `PlanningError`); a fatal
   case (404, exhausted budget) halts the run before
   `fetch_ready` per the existing budget-exhaustion contract.

Cost: one `gh pr view` call per labelled task. For typical
feature sizes (≤ 10 tasks pending merge) this is well under a
second. If the cost becomes meaningful at scale, a future
arc can batch via `gh pr list --search` (the same trick
`list_prs_for_feature` already uses for `turma status`); v1
keeps the per-task lookup because it indexes naturally on
the recorded number.

## State machine

The orchestrator's run-time state machine extends with one
phase between repair and the main loop, and the main loop's
success branch contracts:

```
preflight_check
    └─▶ reconcile (read-only)
            └─▶ repair_phase
                    └─▶ merge_advancement_phase     ← NEW
                            ├─[any halt-on-exhaust]─▶ END_fail
                            └─▶ fetch_ready
                                    ├─[empty]─▶ END (success)
                                    └─[ready]─▶ claim_task
                                                    └─▶ ensure_worktree
                                                            └─▶ run_worker
                                                                    ├─[success marker]─▶ git_commit_push
                                                                    │                           ├─[clean tree]──▶ fail_task
                                                                    │                           ├─[push fail]───▶ fail_task
                                                                    │                           └─[ok]──▶ open_pr
                                                                    │                                       └─▶ mark_pr_open    ← was close_task + cleanup
                                                                    │                                               └─▶ fetch_ready
                                                                    ├─[fail marker]──────▶ fail_task
                                                                    └─[timeout]─────────▶ fail_task
```

Notable shape changes vs the post-Task-7-of-swarm-orchestration
diagram:

- `merge_advancement_phase` exists. Its handlers can short-
  circuit to `END_fail` on exhausted-budget failure (same as
  repair-phase failures already do today).
- The success branch's tail no longer reaches `close_task` or
  `cleanup_worktree` directly. Both move to the
  merge-advancement phase, fired by a future invocation
  observing the merge.

## Adapter additions (typed wrappers, no new bd / gh argv shapes)

### `BeadsAdapter.mark_pr_open(task_id, pr_number)`

```python
def mark_pr_open(self, task_id: str, pr_number: int) -> None:
    """Record a `turma-pr:<N>` label on `task_id`.

    Called from the orchestrator's success path immediately
    after `open_pr` returns. Pairs with `unmark_pr_open`
    (called by the merge-advancement phase before
    `close_task` or `fail_task`) so the label is always
    cleared on terminal transitions.

    argv: `bd update <task_id> --add-label turma-pr:<N>`.
    Reuses bd's existing label mechanism; same shape as the
    `turma-retries:<n>` and `needs_human_review` labels
    already managed by `fail_task`.
    """
```

### `BeadsAdapter.unmark_pr_open(task_id, pr_number)`

```python
def unmark_pr_open(self, task_id: str, pr_number: int) -> None:
    """Remove the `turma-pr:<N>` label from `task_id`.

    Called by the merge-advancement phase before any
    terminal transition (`close_task` on MERGED,
    `fail_task` on CLOSED-without-merge). Idempotent against
    a missing label — bd's `--remove-label` is a no-op when
    the label isn't present.

    argv: `bd update <task_id> --remove-label turma-pr:<N>`.
    """
```

Both methods are tiny wrappers around the same argv pattern
`fail_task` already uses, but typed at the orchestrator's
boundary so the orchestrator code doesn't reach into raw
label strings.

### `PullRequestAdapter.get_pr_state_by_number(pr_number)`

```python
@dataclass(frozen=True)
class PrState:
    number: int
    state: str  # OPEN | MERGED | CLOSED | DRAFT
    url: str    # for the operator-facing log line


def get_pr_state_by_number(self, pr_number: int) -> PrState:
    """Look up a PR by number and return its current state.

    argv: `gh pr view <pr_number> --json number,state,url`.

    Non-zero exit raises `PlanningError`; the
    "PR not found" 404 case (gh's stderr says "no pull
    requests found") is recognized and surfaces as a typed
    `PlanningError` with a hint pointing the operator at
    `bd show <task_id>` for triage rather than a raw stderr
    dump.
    """
```

`list_prs_for_feature` (from the turma-status arc) covers
the feature-scoped batch case but indexes by branch and
returns all states in one call. The merge-advancement sweep
indexes by **PR number** (recorded on the bd label), which is
a different access pattern. Reusing `list_prs_for_feature`
would mean iterating the full PR list and filtering by
number — workable but not idiomatic. A direct number lookup
is cheaper and clearer.

## Repair phase changes (mirror the new model)

The reconciliation module is unchanged — it still classifies
prior-run state into the existing six findings. But the
**repair actions** for two of those findings shift:

### `completion-pending` → label-and-defer (was: close_task + cleanup)

Old `_complete_pending_task` flow:

1. `commit_all`
2. `push_branch`
3. `open_pr` → captures URL
4. `close_task`
5. `cleanup`

New flow:

1. `commit_all`
2. `push_branch`
3. `open_pr` → captures number + URL
4. `mark_pr_open(task_id, number)`

(stops at step 4; the merge-advancement phase on a future
run handles steps 5–6).

### `completion-pending-with-pr` → label-only

Old:

1. `close_task`
2. `cleanup`

New:

1. Recover the PR number from the existing PR (already
   surfaced by reconciliation's `find_open_pr_url_for_branch`
   call → use the URL to derive number, OR add a small
   helper).
2. `mark_pr_open(task_id, number)`

(merge-advancement phase handles close + cleanup later.)

These two changes preserve the v1 invariant that
`close_task` and `cleanup_worktree` only fire after a PR
merge has been observed.

## Error surface

All failures raise `PlanningError`, consistent with the rest
of the swarm. Categories specific to merge-advancement:

- `gh pr view` non-zero exit on a number that doesn't exist
  → `PlanningError("PR #<N> not found via gh; turma-pr label
  on <task_id> is stale. Triage with `bd show <task_id>` and
  `gh pr list --head task/<feature>/<task_id>`.")`. Halts
  the run before `fetch_ready`.
- `gh pr view` malformed payload → `PlanningError` with the
  raw stderr / stdout, same shape as the existing PR adapter
  error surface.
- Budget-exhausted failure inside the sweep (a PR-closed-
  without-merge case where the task was already at retry
  ceiling) → halt with the same terminal `PlanningError` the
  main loop uses today, naming the affected task ids.

The sweep prints one operator-facing line per task it
processed:

```
merge-advancement: smoke-1op → MERGED, closed
merge-advancement: smoke-7fp → CLOSED without merge → fail_task
merge-advancement: smoke-3m6 → OPEN, leave alone
merge-advancement: smoke-9zz → 404; halting (label is stale; triage)
```

Same format as repair-phase output, prefixed with
`merge-advancement:` so the source is unambiguous.

## Tests

All existing scenarios update to the new contract. Categories:

1. **Adapter argv pinning** for the three new methods
   (`mark_pr_open`, `unmark_pr_open`, `get_pr_state_by_number`).
2. **Merge-advancement happy path** — single in_progress task
   with `turma-pr:<N>` label, `gh` returns MERGED → assert
   the sequence `unmark_pr_open` → `close_task` → `cleanup`,
   no other adapter calls.
3. **Merge-advancement OPEN / DRAFT** — task untouched; sweep
   continues; main loop runs normally.
4. **Merge-advancement CLOSED-without-merge** — `unmark_pr_open`
   → `fail_task` with the canned reason; budget remaining
   returns to open, exhausted halts.
5. **Merge-advancement multi-task sweep** — three labelled
   tasks across MERGED / OPEN / CLOSED states; assert each
   handler fires once and in the correct order.
6. **Merge-advancement 404** — `gh pr view <N>` returns
   "no pull requests found"; sweep raises `PlanningError`;
   no other adapter mutations fire after the failure.
7. **`--dry-run` is read-only across the sweep** — dry-run
   queries PR state but performs no `unmark_pr_open` /
   `close_task` / `cleanup` / `fail_task`; the existing
   no-mutation invariant test extended to cover the new
   phase.
8. **Success-path test updates** — existing
   `test_single_task_happy_loop` and friends update from
   "expects `close_task`" to "expects `mark_pr_open`". The
   `closed` list assertions move to the merge-advancement
   tests where they belong.
9. **Repair-action updates** —
   `test_repair_completion_pending_runs_commit_push_pr_close`
   becomes `_runs_commit_push_pr_label`; the
   `completion-pending-with-pr` test updates similarly.
10. **`turma status` rendering picks up PR state inline** —
    in-progress section gains a `pr: #<N> (<state>)` line
    when the recorded label is present.

## Open items deferred past this change

- **Background polling.** A daemon-mode that watches PRs
  without operator intervention is a future capability;
  v1's `turma run`-as-trigger model intentionally keeps the
  human in the loop.
- **Cross-feature merge advancement.** The sweep is
  feature-scoped. A multi-feature dashboard / sweeper is a
  follow-on once `turma status` grows a global view.
- **Mergeable-state diagnostics.** `gh pr view` exposes
  `mergeStateStatus` (CLEAN / DIRTY / BLOCKED / etc.); v1
  ignores it. A future arc could surface "PR is BLOCKED
  awaiting reviewer" in `turma status`.
- **Auto-rebase on conflict.** If the PR's mergeable state
  is CONFLICTING, v1 leaves it alone. Auto-rebasing is a
  bigger workflow change (touches commit history) and
  belongs to a separate spec.
- **Telemetry.** The merge-advancement phase has natural
  hooks for "time-from-PR-open to PR-merge" metrics; v1
  prints log lines only.
