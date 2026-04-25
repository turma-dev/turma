## Scope

Stabilization follow-up to `swarm-post-merge-advancement`.
Fixes three concrete findings the 2026-04-25 smoke surfaced
without expanding the v1 contract:

1. Reconciliation must skip merge-tracked tasks instead of
   re-classifying them as `completion-pending` after the PR
   merges between runs.
2. `mark_pr_open` must replace any prior `turma-pr:<M>`
   label rather than adding alongside, so closed tasks
   don't accumulate stale labels.
3. The orchestrator must fast-forward local
   `<base_branch>` from origin once per run before claiming
   any task, so dependent tasks see the merged base.
   Implementation: a single `git fetch origin
   <base_branch>:<base_branch>` (the colon-form
   fast-forwards a non-checked-out ref or fails loudly on
   divergence). See "`GitAdapter.fetch_and_ff_base`" below
   for why this beats the two-call `fetch + merge --ff-only`
   alternative.

Out of scope: the orphan-branch finding (still deferred),
parallel claims, multi-feature scheduling, daemon-mode
polling, and any further reconciliation surgery beyond the
label-aware skip.

## Why a single arc, not three branches

Findings 1 and 2 are coupled: Finding 1's fix (skip
merge-tracked tasks in reconciliation) prevents the duplicate
`mark_pr_open` callsite that produces Finding 2's
accumulating labels. But Finding 2's safety-net fix
(set-of-one `mark_pr_open`) is still worth shipping because
it prevents accumulation under any future callsite that adds
a label, including operator scripts and future repair paths.
Shipping them together keeps the contract coherent.

Finding 3 is independent in mechanism but tightly coupled in
user-visible behavior: without the fetch fix, Findings 1+2's
merge-tracked task transitions appear to work but the
dependent fails downstream, which reads as "merge advancement
broken". Operators would (rightly) re-file the bug. Bundling
the fetch with the label fixes lets a single re-run of the
chained smoke serve as the validation gate for all three.

## Reconciliation: skip vs new finding

The label-aware fix has two viable shapes:

### Option A — skip during classification (chosen)

Reconciliation's classification loop tests
`_extract_pr_number(snapshot.labels)` first; if non-None,
the task is skipped (no finding emitted, no entry in the
report). A single info-level log line per skipped task
records the routing decision:

```
reconcile: smerge-4k5 → skipped (merge-tracked at PR #5)
```

Repair phase iterates findings; with no finding for the
skipped task, no repair fires. Merge-advancement continues
to query bd directly via `list_in_progress_tasks` +
`_extract_pr_number` — its dispatch is unchanged.

Pros: minimal change to `ReconciliationReport` shape (no new
finding type), clear separation of responsibilities, repair
phase contract stays "every finding has exactly one repair
action".

Cons: reconciliation no longer reports on the full
in-progress set in its returned tuple. Mitigated by the log
line + the existing `turma status` pr-line surfacing.

### Option B — new `MergeTracked` finding type

A new typed finding `MergeTracked(task_id, pr_number)`
emitted by reconciliation; the repair phase has an explicit
no-op handler for it; merge-advancement consumes the report
instead of re-querying bd.

Pros: keeps reconciliation's "every in-progress task gets a
finding" property; surfaces merge-tracked tasks in
programmatic readouts.

Cons: adds a finding type for a state that has no repair
action — every other finding type was paired with a
mutation. The repair-phase handler is defensive scaffolding
that does nothing. Couples merge-advancement to the
reconciliation report (today they're decoupled — sweep
queries bd independently).

**This change picks Option A.** Reasoning: simpler, smaller
diff, and the user-facing surface (`turma status`) already
renders merge-tracked state via the `pr:` line. The lost
"every task gets a finding" property is intentional — the
contract this arc establishes is "merge-tracked tasks are
not reconciliation's territory at all".

## `mark_pr_open` becomes set-of-one

Today's contract (post-idempotency-precheck):

```
1. Read labels via `bd show --json`.
2. If `turma-pr:<N>` already present → return (idempotent skip).
3. Else `bd update --add-label turma-pr:<N>`.
```

New contract:

```
1. Read labels via `bd show --json`.
2. If `turma-pr:<N>` already present:
   - If any other `turma-pr:<M>` label is also present
     (M != N), remove each via `bd update --remove-label`.
   - Return.
3. Else:
   - For each `turma-pr:<M>` (M != N) found in step 1:
     `bd update --remove-label turma-pr:<M>`.
   - `bd update --add-label turma-pr:<N>`.
```

The label invariant becomes: **at any time, a bd task
carries at most one `turma-pr:` label, and that label
matches the PR currently tracked for the task.**

Edge cases tests must cover:
- Task with no prior `turma-pr:` → single `--add-label`,
  same as today.
- Task with `turma-pr:5`, `mark_pr_open(task, 5)` → no-op
  (precheck skip), no removal calls.
- Task with `turma-pr:5`, `mark_pr_open(task, 6)` →
  `--remove-label turma-pr:5` then `--add-label turma-pr:6`.
- Task with `turma-pr:5` AND `turma-pr:6` (corruption from
  a pre-fix run), `mark_pr_open(task, 7)` → two removes
  then one add. Order of removes is deterministic but not
  load-bearing.
- Task with `turma-pr:5` AND `turma-pr:6`,
  `mark_pr_open(task, 5)` → precheck-skip path with
  cleanup: remove `turma-pr:6`, return. Pin this so
  re-issuing `mark_pr_open` against an already-correct task
  also cleans up corruption.

`unmark_pr_open` semantics are unchanged. With the
set-of-one invariant, `unmark_pr_open(task, N)` removes the
single label and the task is label-free — no orphan
remains.

## `GitAdapter.fetch_and_ff_base`

New method, scoped narrowly:

```python
def fetch_and_ff_base(self, base_branch: str) -> None:
    """Fast-forward local `<base_branch>` from origin.

    argv (single call):
      git -C <repo_root> fetch origin <base_branch>:<base_branch>

    The colon-form refspec fetches `origin/<base_branch>`
    and fast-forwards local `<base_branch>` to match in one
    operation. If local has diverged, the call fails with
    `non-fast-forward` and no merge is attempted. The
    operator's current HEAD / checked-out branch is not
    disturbed.
    """
```

### Why the single colon-form, not `fetch + merge --ff-only`

The natural alternative is two calls:

```
git -C <repo_root> fetch origin <base_branch>
git -C <repo_root> merge --ff-only origin/<base_branch>
```

Two reasons the colon-form wins:

1. **The merge step requires HEAD to be on
   `<base_branch>`.** The orchestrator runs from the
   operator's current shell — they may be on a feature
   branch with local edits. A `git checkout <base_branch>`
   first would disturb that, and reverting at the end
   adds failure modes mid-flow. The colon-form fetches
   into the named local ref directly without touching
   HEAD.

2. **Single subprocess = single failure surface.** Two
   calls means partial failure (fetch succeeded, merge
   refused) is a real intermediate state. The colon-form
   either updates the ref or doesn't, no in-between.

The `fetch + merge` shape is rejected on these grounds.

### Failure modes

- Network / origin unreachable → `git fetch` non-zero exit.
  Surface as `PlanningError` preserving stderr verbatim.
- Local `<base_branch>` has diverged from origin →
  colon-form fetch refuses with `! [rejected]` /
  `non-fast-forward` in stderr. Surface as a typed
  `PlanningError` that names the branch and points the
  operator at `git log <base_branch>..origin/<base_branch>`
  and `git log origin/<base_branch>..<base_branch>` for
  triage.
- The local ref doesn't exist yet (fresh clone hasn't
  checked out `<base_branch>`) → the colon-form creates it.
  v1 OK.

Argv-pin tested in `tests/test_swarm_git.py`. Existing
`GitAdapter` test pattern (subprocess stub) carries over.

## Orchestrator integration

Call site: top of `run_swarm`, after preflight, before
reconciliation:

```
preflight → fetch_and_ff_base → reconcile → repair
  → merge_advancement → main_loop
```

`--dry-run` skips `fetch_and_ff_base` (fast-forward updates
a local ref, which is a state mutation). The dry-run readout
prints a single line indicating the skip:

```
fetch: skipped (--dry-run)
```

Non-dry-run prints:

```
fetch: origin/main → main (already up to date)
```
or
```
fetch: origin/main → main (advanced 3 commits)
```

Test coverage:
- Happy path: stub `git` reports success → `run_swarm`
  proceeds.
- Network failure: stub raises `PlanningError` → halt
  before reconcile, no reconcile call recorded.
- Divergent local: stub raises typed PlanningError → halt
  with the operator-facing message intact.
- Dry-run: `fetch_and_ff_base` not called; rest of the
  read-only sweep behaves as today.

## Error surface

All failures continue to raise `PlanningError`. Categories
specific to this arc:

- `git fetch` non-zero exit on a divergent local
  → `PlanningError("local <base_branch> has diverged from
    origin/<base_branch>; refusing to fast-forward.
    Triage with `git log <base_branch>..origin/<base_branch>`
    and the reverse to compare.")`
- `git fetch` non-zero exit on network / auth failure →
  `PlanningError` preserving git's stderr.

No new error categories from the reconciliation skip or the
`mark_pr_open` set-of-one fix — both are pure refinements of
existing surfaces.

## State-machine effect

```
preflight (unchanged)
  → fetch_and_ff_base                  <- NEW (skipped on --dry-run)
  → reconcile (now skips merge-tracked tasks)
  → repair (no findings emitted for merge-tracked tasks)
  → merge_advancement (unchanged dispatch)
  → main_loop (unchanged; worktrees now start from up-to-date base)
```

The merge-tracked task lifecycle becomes:

```
mark_pr_open(task, N)                  <- run K, end of success path
  ... operator merges PR on GitHub ...
fetch_and_ff_base                       <- run K+1, top of run_swarm
reconcile: skip (merge-tracked)         <- run K+1
merge_advancement: MERGED, closed       <- run K+1
unmark_pr_open(task, N)                 <- run K+1
close_task(task)                        <- run K+1
cleanup_worktree                        <- run K+1
[dependents become ready, main_loop claims them]
```

No duplicate PRs, no stale labels on closed tasks, no
worktree set up from stale base.

## Tests

Categories the implementation tasks must cover:

1. **Reconciliation skip behavior**: in_progress task with
   `turma-pr:<N>` label is not classified into any of the
   six findings; reconciliation report omits it; the skip
   log line fires.
2. **Reconciliation no longer false-positives merged-PR
   tasks**: regression test for Finding 1. Stub
   reconciliation fixture: in_progress task with
   `.task_complete` AND `turma-pr:<N>` AND no open PR for
   the branch (because the PR is merged) — assert the task
   is skipped, NOT classified as `completion-pending`. The
   repair phase records zero calls for this task.
3. **`mark_pr_open` set-of-one**: argv pin for the
   replace-existing path; argv pin for the no-op path with
   cleanup; idempotent re-call against an already-correct
   task; multi-stale-label cleanup; existing tests carry
   over unchanged.
4. **`GitAdapter.fetch_and_ff_base`**: argv pin; happy path;
   network failure surface; divergent-local surface; the
   colon-form fast-forward (not fetch + merge).
5. **`run_swarm` integration**: fetch step appears at the
   top of the call sequence; halts before reconcile on
   fetch failure; skipped under `--dry-run`; the dry-run
   no-mutation invariant test extended to cover this skip.
6. **End-to-end fixture for the chained-feature flow**: the
   regression for the smoke's failure mode. A two-task
   chain where task 1 has `turma-pr:<N>` + the PR is
   MERGED; assert run sequence is fetch → reconcile (skip
   task 1) → repair (no-op on task 1) →
   merge-advancement (close task 1) → main_loop claims
   task 2. No duplicate PR is opened.

## Open items deferred past this change

- **Reconciliation reporting symmetry.** Whether to add a
  `MergeTracked` finding type for programmatic
  consumers is left open. v1's three callers
  (`_apply_repairs`, `turma status`, the orchestrator's
  log) all have what they need from the skip + log
  approach.
- **Operator-driven label cleanup.** A `turma run --triage`
  flag that scans bd for stale `turma-pr:` labels on closed
  tasks (corruption from pre-fix runs) is deferred. Once
  this arc ships, no new accumulation occurs; existing
  corruption can be hand-fixed via `bd update --remove-label`.
- **Pull strategy beyond fast-forward.** v1 deliberately
  refuses to merge a divergent local. A future operator
  workflow might want a "rebase local on origin" path; that
  is a separate decision about turma's posture toward
  operator commits on `<base_branch>`.
- **Per-worktree fetch.** Today the fetch is once per
  `run_swarm`. A future arc that supports parallel claims
  may need per-claim fetches. Not load-bearing for the
  sequential v1 loop.
