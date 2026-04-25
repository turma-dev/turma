## Tasks

### 1. Add `BeadsAdapter.mark_pr_open` / `unmark_pr_open`

- [x] Add `mark_pr_open(task_id: str, pr_number: int) -> None`
      to `BeadsAdapter` in `src/turma/transcription/beads.py`.
      argv pinned: `bd update <task_id> --add-label
      turma-pr:<N>`.
- [x] Add `unmark_pr_open(task_id: str, pr_number: int) ->
      None`. argv pinned: `bd update <task_id> --remove-label
      turma-pr:<N>`. Both methods raise `PlanningError` on
      non-zero exit, matching the existing label surface used
      by `fail_task`.
- [x] Module-level constant `PR_LABEL_PREFIX = "turma-pr:"` so
      the label string is defined once. Mirrors the existing
      `RETRIES_LABEL_PREFIX` constant.
- [x] Add a small helper `_extract_pr_number(labels) ->
      int | None` (parallel to the existing
      `_parse_retries_from_labels`) so the merge-advancement
      sweep doesn't reach into label string parsing
      directly.
- [x] Tests in `tests/test_transcription_beads.py`: argv pin
      for both methods; `_extract_pr_number` returns `None`
      when no `turma-pr:` label is present, returns `N` when
      one is, ignores malformed values
      (`turma-pr:not-a-number` → None), and picks the first
      one when somehow multiple are present.

### 2. Add `PullRequestAdapter.get_pr_state_by_number`

- [x] Add a frozen `PrState(number: int, state: str, url:
      str)` dataclass to `src/turma/swarm/pull_request.py`.
      `state` preserved as-returned by `gh`'s `--json state`
      vocabulary: `OPEN` / `MERGED` / `CLOSED`. v1 does not
      query `isDraft`; draft PRs return `state == "OPEN"`
      and are treated identically to non-draft open PRs.
- [x] Add `get_pr_state_by_number(pr_number: int) -> PrState`.
      argv pinned: `gh pr view <pr_number> --json
      number,state,url`.
- [x] Non-zero exit raises `PlanningError`. The
      "no pull requests found" / 404 case is recognized by
      checking `result.stderr` for the gh-canonical phrase;
      surfaces with a typed message that names the missing
      PR number and points the operator at `bd show
      <task_id>` for triage.
- [x] Non-JSON / non-dict payloads raise `PlanningError`.
- [x] Tests in `tests/test_swarm_pull_request.py`:
      argv shape; happy-path parses a single payload across
      each of the three `gh --json state` values
      (`OPEN` / `MERGED` / `CLOSED`; drafts surface as `OPEN`
      and are not a separate state in v1); 404 path produces
      the typed "PR <N> not found" error; non-zero non-404
      exit surfaces stderr verbatim; non-JSON / non-dict
      rejection.

### 3. Switch the success path to label-and-leave-in-progress

- [x] In `src/turma/swarm/_orchestrator.py`'s
      `_run_single_task`, replace the
      `services.beads.close_task(task.id)` +
      `services.worktree.cleanup(ref)` tail (after a
      successful `open_pr`) with a single
      `services.beads.mark_pr_open(task.id,
      _pr_number_from_url(pr_url))` call. The task remains
      `in_progress` and the worktree stays on disk.
- [x] Add a small helper `_pr_number_from_url(url: str) ->
      int` that parses GitHub PR URLs (the URL `gh pr
      create` returns is the canonical
      `https://github.com/<owner>/<repo>/pull/<N>` shape).
      Raise `PlanningError` on a URL that doesn't match the
      pattern; the orchestrator depends on the number.
- [x] Operator-facing log line updated: replace
      `swarm: closed <id> (PR: <url>)` with
      `swarm: opened <id> (PR: <url>; awaiting merge)` so
      the new contract is unambiguous in the run log.
- [x] Tests in `tests/test_swarm_run.py`:
      `test_single_task_happy_loop` updates from
      `assert beads.closed == ["bd-1"]` to
      `assert beads.pr_marked == [("bd-1", <N>)]`. Stub
      adapter gains a `pr_marked` list to record
      `mark_pr_open` calls. Existing `closed` assertion
      moves to the merge-advancement happy-path test (Task
      4). Other happy-path tests (`test_multi_task_*`,
      `test_max_tasks_caps_iterations`,
      `test_claim_race_skips_raced_task_and_continues`)
      update equivalently.
- [x] Tests in `tests/test_swarm_run.py` for
      `_pr_number_from_url`: parses a canonical URL,
      raises on non-PR URLs, raises on URLs missing the
      number suffix.

### 4. Add the merge-advancement phase

- [x] New `_advance_merged_prs(feature, services, *,
      dry_run)` function in
      `src/turma/swarm/_orchestrator.py`, called from
      `run_swarm` between `_apply_repairs(...)` and
      `_main_loop(...)`. Skipped when `dry_run=True`
      apart from the PR-state reads — see Task 6 for the
      dry-run invariant test.
- [x] Lists `services.beads.list_in_progress_tasks(feature)`,
      filters to tasks whose labels carry a
      `turma-pr:<N>` marker (via the
      `_extract_pr_number` helper from Task 1), and for
      each calls `services.pr.get_pr_state_by_number(N)`.
- [x] Per-task dispatch:
      - `state == "MERGED"` →
        `services.beads.unmark_pr_open(task.id, N)` →
        `services.beads.close_task(task.id)` →
        `services.worktree.cleanup(_ref_for(...))`.
      - `state == "OPEN"` → log only. (Draft PRs return
        `state == "OPEN"` from `--json state` and fall
        through this branch unchanged.)
      - `state == "CLOSED"` (mergedAt null is implied —
        `state == "CLOSED"` and not "MERGED" is sufficient)
        → `services.beads.unmark_pr_open(task.id, N)` →
        `_handle_failure(services, task.id,
        f"PR #{N} closed without merge")`. Track exhausted
        ids identical to `_apply_repairs`'s pattern.
      - `gh pr view` raises a typed "PR not found" error →
        re-raise as `PlanningError` with the canned triage
        message; halt before `fetch_ready`.
- [x] After the per-task loop, raise the existing
      "retry budget exhausted on …" `PlanningError` if any
      task exhausted during the sweep — same shape as
      `_apply_repairs`.
- [x] Operator-facing log lines per finding:
      `merge-advancement: <id> → MERGED, closed`,
      `merge-advancement: <id> → OPEN, leaving alone`,
      `merge-advancement: <id> → CLOSED without merge → fail_task`.
- [x] **Closed-task labels are out of scope for this sweep.**
      The sweep input is strictly `list_in_progress_tasks` —
      a stale `turma-pr:<N>` label on a closed task is not
      detected here. See `design.md` "Deferred: stale
      turma-pr label on a closed bd task" for the rationale.
      Do not silently broaden the sweep input to
      `list_feature_tasks_all_statuses` to catch the case;
      that's a separate spec if it ever becomes load-bearing.

### 5. Tests for the merge-advancement phase

- [x] New tests in `tests/test_swarm_run.py`:
      - **MERGED happy path:** one labelled task, gh returns
        MERGED, assert `unmark_pr_open` then `close_task`
        then `cleanup` calls in that order, no other mutations.
      - **OPEN leaves alone:** labelled task, gh OPEN, assert
        no Beads / worktree mutation; main loop runs normally
        afterwards.
      - **Draft PRs treated as OPEN.** `gh`'s `--json state`
        returns `OPEN` for draft PRs (draftness lives on a
        separate `isDraft` boolean v1 does not query). The
        existing OPEN test covers this; pin a fixture
        comment so the relationship is explicit.
      - **CLOSED without merge → fail_task with retry
        budget remaining** (returns task to open).
      - **CLOSED without merge → exhausted budget halts**
        (raises terminal `PlanningError`).
      - **Multi-task sweep:** three labelled tasks across
        MERGED / OPEN / CLOSED, each handler fires once in
        order; main loop reaches `fetch_ready` only because
        no exhaustion fired.
      - **PR not found (404):** sweep raises
        `PlanningError` with the triage hint; no
        post-failure mutations on remaining tasks.
      - **No-mutation invariant under `--dry-run`:** the
        existing `test_dry_run_never_calls_any_mutation`
        test is extended so the merge-advancement phase
        also performs zero mutations on every stub when
        `dry_run=True`.
      - **In_progress task without a `turma-pr:` label is
        ignored by the sweep** (no `get_pr_state_by_number`
        call for it). Documents that "no label" is the
        reconciliation module's territory, not merge-
        advancement's.

### 6. Repair-phase updates

- [x] In `_complete_pending_task` (`_orchestrator.py`),
      replace the `services.beads.close_task(task_id)` +
      `services.worktree.cleanup(ref)` tail with
      `services.beads.mark_pr_open(task_id,
      _pr_number_from_url(pr_url))`. Same shape change as
      Task 3, mirrored into the repair tail.
- [x] In the `case CompletionPendingWithPr(...)` arm of
      `_apply_repairs`, replace `close_task` + `cleanup`
      with `mark_pr_open(task_id,
      _pr_number_from_url(pr_url))`. The existing PR URL
      from the reconciliation finding is the source of N.
- [x] Log lines updated to match the new contract:
      `repair: <id> → committed, pushed, PR opened
      (<url>; awaiting merge)` and
      `repair: <id> → labelled (PR already open at <url>;
      awaiting merge)`.
- [x] Tests in `tests/test_swarm_run.py`:
      - `test_repair_completion_pending_runs_commit_push_pr_close`
        renamed to
        `test_repair_completion_pending_runs_commit_push_pr_label`;
        assertion swaps from `close_task` + `cleanup` to
        `mark_pr_open`.
      - `test_repair_completion_pending_with_pr_closes_and_cleans`
        renamed and assertion updated to expect
        `mark_pr_open` + leave-alone.
      - The two repair tests' "no-orphan-cleanup" /
        "task-closed-immediately" assertions become
        "task-still-in_progress + label-set" assertions.

### 7. `turma status` in-progress section: surface PR + state

- [x] In `src/turma/swarm/status.py`'s in-progress
      rendering, when a task carries a `turma-pr:<N>`
      label, query the PR state via
      `services.pr.get_pr_state_by_number(N)` and add a
      `pr: #<N> (<state>) <url>` line under the existing
      worktree / sentinel lines.
- [x] When the label is absent, no extra line — the
      in-progress section renders as today.
- [x] Adapter call lives behind the existing no-mutation
      invariant; `get_pr_state_by_number` is read-only.
      Extend the no-mutation headline test fixture to
      cover this addition.
- [x] Tests in `tests/test_swarm_status.py`:
      - In-progress task with a `turma-pr:<N>` label and
        gh returning OPEN renders the new line.
      - Same task with gh returning MERGED renders too
        (with state=MERGED so the operator sees it's
        ready for the next `turma run` to advance).
      - In-progress task without the label renders the
        existing three lines only (no PR line).
      - `gh pr view` failure during the readout
        propagates as `PlanningError` (no partial readout
        rule still applies).

### 8. Docs + CHANGELOG

- [x] `README.md` Swarm Execution section:
      - Update the "one-feature loop" subsection: success
        path stops at `open_pr` + label; close + cleanup
        defer to the next run's merge-advancement sweep.
      - Add a "Merge advancement" subsection covering the
        new phase, its read-only-then-mutate contract, and
        the four PR states it dispatches on.
      - Update the worked example: show two consecutive
        `turma run` invocations, the first opening a PR,
        the second observing the merge and advancing the
        DAG.
- [x] `docs/architecture.md` Execution section: extend the
      state machine diagram with the
      `merge_advancement_phase` node.
- [x] `docs/smoke-turma-run.md`: new step between the
      existing happy-path step and the failure step
      demonstrating
      `gh pr merge` + re-run `turma run` → observe
      merge-advancement close the bd task and unblock a
      dependent.
- [x] `CHANGELOG.md` `[Unreleased]`: roll-up entry under
      "Added" describing the new phase + the contract
      shift (success path no longer closes immediately).

### 9. Validation

- [x] `uv run pytest` green. Current baseline before this
      change set: 469 tests. Expected net add: ~25 across
      adapter additions, merge-advancement coverage, status
      section, and updated existing tests.
- [ ] Manual smoke against the existing
      `khanhgithead/turma-run-smoke` scratch (left unchecked
      until the operator walks the runbook end-to-end against
      the live scratch; the new Step 3 in
      `docs/smoke-turma-run.md` covers this):
      - Pre-stage a fresh feature with two dependency-
        chained tasks (the prior smoke runs only used
        single-task specs).
      - Run `turma run --feature smoke-merge --max-tasks 1`
        — opens PR for task 1, leaves it in_progress with
        `turma-pr:<N>`. Confirm via
        `bd show <task-1>` that the task is still
        in_progress and the label is present.
      - Manually `gh pr merge <N>` (squash, since the
        scratch repo is private and squash matches the
        smoke's existing pattern).
      - Run `turma run --feature smoke-merge --max-tasks 1`
        again. Expect: `merge-advancement: <task-1> →
        MERGED, closed`, then the dependent claim runs
        normally.
      - Confirm `bd show <task-1>` is closed; dependent
        becomes ready; PR for task 2 opens.
      - Document any surprises as follow-up tasks.
- [x] No new runtime deps in `pyproject.toml`. `bd` and
      `gh` already prerequisites.
