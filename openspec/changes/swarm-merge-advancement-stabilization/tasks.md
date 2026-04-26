## Tasks

### 1. Reconciliation skips merge-tracked tasks

- [x] In `src/turma/swarm/reconciliation.py`, modify the
      classification loop so that any in_progress task with
      a valid `turma-pr:<N>` label
      (via `_extract_pr_number`) is skipped before the
      worktree / sentinel / PR checks fire. The task is not
      added to any of the report's finding lists.
- [x] Reconciliation prints one log line per skipped task:
      ```
      reconcile: <id> → skipped (merge-tracked at PR #<N>)
      ```
      Format must be parseable by the same line-prefix
      pattern the existing `reconcile:` lines use.
- [x] Update `reconciliation.py`'s module docstring to
      document the skip behavior and link to this change's
      `design.md` "Reconciliation: skip vs new finding"
      subsection. The "every in-progress task gets a
      finding" property is explicitly retired.
- [x] Tests in `tests/test_swarm_reconciliation.py`:
      - in_progress task with `turma-pr:<N>` label is
        absent from every finding list in the returned
        report.
      - The skip log line fires once per skipped task with
        the correct PR number.
      - Multiple in-progress tasks: a mix of merge-tracked
        and non-merge-tracked tasks is classified
        correctly (skip the labelled ones, classify the
        rest as today).
      - **Regression for Finding 1**: in_progress task with
        `.task_complete` on disk AND `turma-pr:<N>` AND no
        open PR for the branch — assert NOT classified as
        `completion-pending`. The repair phase records
        zero calls for this task.
      - in_progress task with a malformed `turma-pr:` label
        (e.g. `turma-pr:not-a-number`) falls through to
        normal classification — `_extract_pr_number`
        returning None means "not merge-tracked".

### 2. `mark_pr_open` becomes set-of-one

- [x] In `src/turma/transcription/beads.py`, modify
      `mark_pr_open(task_id, pr_number)`:
      - Read labels via the existing `bd show --json`
        precheck.
      - If `turma-pr:<N>` is already present: also remove
        any other `turma-pr:<M>` (M ≠ N) labels found in
        the same precheck before returning.
      - If `turma-pr:<N>` is absent: remove every other
        `turma-pr:<M>` label found in the precheck, then
        add `turma-pr:<N>`.
      - Each removal is a separate
        `bd update --remove-label turma-pr:<M>` call.
- [x] Update the method's docstring to pin the new
      invariant: **a bd task carries at most one
      `turma-pr:` label at any time, matching the PR
      currently tracked**. Reference the design.md
      subsection.
- [x] Tests in `tests/test_transcription_beads.py`:
      - Existing argv-pin test continues to pass for the
        no-prior-label path (single `--add-label`).
      - New: precheck-skip path where the task has
        `turma-pr:<N>` AND a stale `turma-pr:<M>` (M ≠ N) →
        argv records the precheck + one
        `--remove-label turma-pr:<M>`, no `--add-label`.
      - New: replace path where the task has
        `turma-pr:<M>` and `mark_pr_open(task, N)` is called
        with N ≠ M → argv records precheck +
        `--remove-label turma-pr:<M>` +
        `--add-label turma-pr:<N>` in that order.
      - New: multi-stale path where the task has
        `turma-pr:<M>` and `turma-pr:<K>` and
        `mark_pr_open(task, N)` is called → both stale
        labels removed before the new label is added.
      - Idempotent re-call: two consecutive
        `mark_pr_open(task, N)` calls with no other state
        change → second call is precheck-skip with no
        removes.

### 3. `GitAdapter.fetch_and_ff_base`

- [x] Add `fetch_and_ff_base(base_branch: str) -> None` to
      `GitAdapter` in `src/turma/swarm/git.py`. argv pinned:
      ```
      git -C <repo_root> fetch origin <base_branch>:<base_branch>
      ```
      The colon-form fast-forwards the local ref or fails
      loudly on divergence — single subprocess call, no
      separate merge step.
- [x] Failure mapping:
      - Non-zero exit with stderr containing
        `non-fast-forward` or `rejected` →
        `PlanningError("local <base_branch> has diverged
        from origin/<base_branch>; refusing to fast-forward.
        Triage with `git log
        <base_branch>..origin/<base_branch>` and the
        reverse.")`. The branch name is interpolated.
      - Any other non-zero exit → `PlanningError` preserving
        git's stderr verbatim, prefixed with
        `git fetch failed:`.
- [x] Tests in `tests/test_swarm_git.py`:
      - argv shape pin (subprocess stub records the call).
      - Happy path returns None on zero exit.
      - Divergent-local → typed PlanningError with the
        triage hint.
      - Network / auth failure → PlanningError with
        stderr preserved.

### 4. Orchestrator calls `fetch_and_ff_base` once per run

- [x] In `src/turma/swarm/_orchestrator.py`'s `run_swarm`,
      call `services.git.fetch_and_ff_base(services.base_branch)`
      after preflight, before `_reconcile`. Wrap the call
      in a `try/except PlanningError` only if needed for
      log-line dressing — otherwise let it propagate
      (preflight failures already halt the run via
      PlanningError today).
- [x] Skip the call entirely under `--dry-run`. Print
      `fetch: skipped (--dry-run)` on dry-run; print
      `fetch: origin/<base_branch> → <base_branch>` on the
      non-dry-run happy path. (No need to compute
      "advanced N commits" — that's cosmetic and v1 omits
      it.)
- [x] Tests in `tests/test_swarm_run.py`:
      - Happy path test: `fetch_and_ff_base` is called
        before reconcile.
      - Fetch failure halts before reconcile: the stub
        records the fetch call, no reconcile / repair /
        sweep / main_loop calls follow.
      - `--dry-run` invariant: the existing
        `test_dry_run_never_calls_any_mutation` test is
        extended so `fetch_and_ff_base` is also recorded as
        not-called under dry-run. Add a separate
        non-dry-run assertion that fetch IS called.

### 5. End-to-end regression test for the chained flow

- [x] In `tests/test_swarm_run.py`, add
      `test_chained_feature_post_merge_advances_dependent`
      that simulates the smoke's failure mode end-to-end
      against stub adapters:
      - Two-task chain (task A blocks task B). Task A is
        `in_progress` with `.task_complete` on disk and
        `turma-pr:<N>` label. The stub `gh` says PR <N> is
        MERGED. No open PR exists for the branch.
      - Run sequence asserted: `fetch_and_ff_base` →
        reconcile (skip task A) → repair (no calls for
        task A) → merge_advancement (unmark + close +
        cleanup task A) → main_loop claims task B → task
        B's worker runs against the worktree → mark_pr_open
        for task B's PR.
      - Critically: `pr_marked` records exactly one entry
        per task (no duplicate label, no extra
        `mark_pr_open` from a repair-phase false-positive).
      - The labels on task A through the run never include
        more than one `turma-pr:` value; final state is
        closed with no `turma-pr:` label remaining.
- [x] This test's assertions form the regression contract
      for Findings 1 + 2 + 3 together. If any single fix
      regresses, this test fails.

### 6. Docs + CHANGELOG

- [x] `README.md` Swarm Execution: insert a
      `fetch_and_ff_base` step at the top of the state
      machine description; one short subsection
      "Base-branch sync" between Prerequisites and the
      one-feature loop.
- [x] `docs/architecture.md` Execution section: extend the
      state-machine diagram with the new
      `fetch_and_ff_base` node before reconcile; add a
      paragraph explaining that v1 refuses divergent local
      bases (operator triages).
- [x] `docs/smoke-turma-run.md`:
      - Step 2's expected log gains the `fetch:` line at
        the top.
      - Step 3's expected log gains the
        `reconcile: ... → skipped (merge-tracked at PR #<N>)`
        line and removes the (incorrect, post-stabilization)
        `repair: ... → committed, pushed, PR opened ...`
        line for the merge-tracked task. Add a new
        sub-step demonstrating a chained feature with two
        tasks so the dependent unblock is observable.
      - Documentation note: the Apr 25 smoke surfaced this
        arc's three findings. Brief paragraph linking to
        this change.
- [x] `CHANGELOG.md` `[Unreleased]/Fixed`: roll-up entry
      naming the three findings, the contract changes
      (label-aware reconcile, set-of-one `mark_pr_open`,
      base-branch fast-forward), and the regression-test
      tie-in.

### 7. Validation

- [x] `uv run pytest` green. Current baseline before this
      change set: 514 tests. Expected net add: ~10–14
      across reconciliation skip tests, set-of-one
      adapter tests, fetch_and_ff_base argv tests,
      orchestrator integration tests, and the chained
      regression test.
- [x] No new runtime deps in `pyproject.toml`. `git`
      already a prerequisite.
- [ ] Manual smoke against `khanhgithead/turma-run-smoke`
      (left unchecked until the operator walks the runbook
      end-to-end against the live scratch; the chained-flow
      sub-step is now Step 3a in
      `docs/smoke-turma-run.md`):
      walk Steps 1 → 2 → 3 with the same two-task chained
      feature (`smoke-merge`-style spec). Expected differences
      from the Apr 25 run:
      - Run 2's reconciliation skips task 1 (no
        `completion-pending` classification, no duplicate
        PR).
      - Task 1's final bd state is closed with no stale
        `turma-pr:` label.
      - Task 2's worktree is set up from up-to-date local
        main; the worker sees STAGE.txt and writes the
        appended line; PR for task 2 opens cleanly.
      - Document any new surprises as follow-up tasks.
