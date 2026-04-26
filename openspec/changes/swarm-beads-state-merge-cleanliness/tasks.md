## Tasks

### 0. Investigate bd-side options before committing the revert approach

- [ ] Confirm by direct test that bd resolves
      `.beads/embeddeddolt/` from main when invoked from
      inside a worktree (i.e. all worktrees share the
      same dolt db). The proposal assumes this; verify
      it on the existing smoke scratch dir or a fresh
      `bd init`-ed tmpdir before relying on the
      assumption.
- [ ] Check whether bd has a flag (`--no-export`,
      `--no-hooks`, or similar) on `bd update` that
      suppresses the post-update jsonl re-export. If it
      exists, this arc's `revert_paths` approach can be
      replaced with passing that flag at every Turma bd
      mutation site — cleaner because the dirty file
      never gets written in the first place.
- [ ] Check whether bd's "worktree redirect file"
      (mentioned in `.beads/.gitignore`) provides a
      different cwd-walkup mechanism inside worktrees.
      Likely irrelevant for this arc (we're not
      moving any mutations to worktrees), but document
      what it does so future arcs can use it if
      needed.
- [ ] No code changes; this is a 5-10 minute scratch-
      dir investigation. Surface findings before
      starting Task 1. If a `--no-export` flag exists,
      the spec gets a final revision to switch from
      revert to flag-passing; the rest of the tasks
      shrink accordingly.

### 1. GitAdapter: add `revert_paths` and `path_is_dirty`

- [ ] In `src/turma/swarm/git.py`, add
      `revert_paths(repo_root: Path, paths: tuple[str,
      ...]) -> None`. argv pinned:
      ```
      git -C <repo_root> checkout -- <p1> <p2> ...
      ```
- [ ] Empty paths tuple → no subprocess call, return
      None immediately. Don't fire git with zero paths
      (it has surprising defaults).
- [ ] Failure mapping: non-zero exit → `PlanningError(
      "git checkout failed: exit <N>\n<stderr>")`.
      Preserves stderr verbatim so operators can read
      the actual git error.
- [ ] Also add `path_is_dirty(repo_root: Path, path:
      str) -> bool`. argv pinned:
      ```
      git -C <repo_root> status --porcelain=v1 -- <path>
      ```
      Returns True iff stdout contains a tracked-file
      modification line for `path` (porcelain prefixes
      ` M`, `M `, `MM`, `A `, etc.). Returns False on
      empty stdout AND on `??`-prefixed lines (untracked
      files at that path are not Turma's business).
- [ ] Tests for `revert_paths` in `tests/
      test_swarm_git.py`:
      - argv-pin: single subprocess.run call with the
        right argv.
      - empty-paths: no subprocess.run call; returns
        None; assert via `mock_run.call_count == 0`.
      - multi-path: paths passed verbatim in argv
        order.
      - failure surfaces stderr.
- [ ] Tests for `path_is_dirty` in
      `tests/test_swarm_git.py`:
      - argv-pin.
      - clean (empty stdout) returns False.
      - modified (` M .beads/issues.jsonl`) returns
        True.
      - staged (`M  .beads/issues.jsonl`) returns
        True.
      - untracked (`?? .beads/issues.jsonl`) returns
        False (operator's untracked file is not
        Turma's concern).
      - failure surfaces stderr as PlanningError.

### 2. Preflight: refuse to start on dirty bd-state file

- [ ] In `src/turma/swarm/_orchestrator.py`, add
      `_preflight_beads_state_clean(services)` helper
      that raises `PlanningError` if
      `services.git.path_is_dirty(services.repo_root,
      ".beads/issues.jsonl")` returns True.
- [ ] The error message names the file and gives the
      three triage commands operators need:
      ```
      .beads/issues.jsonl has uncommitted changes in
      main's working tree. turma run requires this
      file to be clean before starting because it
      manages the file's working-tree state across
      iterations. Triage with:
        git diff .beads/issues.jsonl
        git stash push -- .beads/issues.jsonl
        git checkout -- .beads/issues.jsonl
      ```
- [ ] Call site in `run_swarm`: invoke
      `_preflight_beads_state_clean(services)` after
      the existing `_preflight(...)` (spec/APPROVED/
      TRANSCRIBED checks), before `fetch_and_ff_base`.
      **Skipped under `--dry-run`** since dry-run
      doesn't mutate bd state; dry-run readouts
      against a dirty bd-state file are safe.
- [ ] Tests in `tests/test_swarm_run.py`:
      - New `test_run_swarm_refuses_when_beads_state_dirty`:
        StubGit reports the file dirty → run_swarm
        raises typed PlanningError with the file name
        and the triage commands in the message; no
        further phases (fetch, reconcile, repair,
        sweep, main_loop) run.
      - New `test_dry_run_skips_beads_state_preflight`:
        StubGit reports dirty → dry-run completes
        without raising (the existing `--dry-run`
        no-mutation invariant is unaffected).
      - Existing `test_dry_run_never_calls_any_mutation`
        and `test_single_task_happy_loop` get an
        explicit clean-by-default StubGit response so
        they continue to pass.

### 3. Orchestrator: revert export after each bd mutation

- [ ] In `src/turma/swarm/_orchestrator.py`, add a
      module-private constant `_BEADS_EXPORT =
      (".beads/issues.jsonl",)` and a small helper:
      ```python
      def _revert_beads_export(services: SwarmServices) -> None:
          services.git.revert_paths(
              services.repo_root, _BEADS_EXPORT
          )
      ```
- [ ] Call sites — one revert per Turma bd mutation
      point:
      1. `_run_single_task` after `claim_task`
      2. `_run_single_task` after `mark_pr_open`
         (success path)
      3. `_run_single_task` after `fail_task`
         (failure path)
      4. `_apply_repairs` `CompletionPendingWithPr` arm
         after `mark_pr_open`
      5. `_advance_merged_prs` MERGED dispatch after
         the `unmark_pr_open` + `close_task` pair
         (single revert at end of arm)
      6. `_advance_merged_prs` CLOSED dispatch after
         the `unmark_pr_open` + `_handle_failure`
         pair (single revert at end of arm)
- [ ] Tests in `tests/test_swarm_run.py`:
      - `StubGit.revert_paths` records the call (path
        tuple captured in calls list).
      - `test_single_task_happy_loop` git_steps
        assertion extended to include `revert_paths`
        after `claim_task` and after `mark_pr_open`.
      - New `test_revert_beads_export_runs_after_each_bd_mutation`:
        all six callsite-pairs pinned by inspecting
        StubGit + StubBeads call order.
      - The merge-advancement happy-path tests
        (`test_merge_advancement_merged_path`,
        `test_merge_advancement_closed_without_merge_returns_to_open`,
        `test_merge_advancement_closed_without_merge_exhausts_budget`)
        each assert one revert per dispatch arm.
      - The chained-flow regression test
        (`test_chained_feature_post_merge_advances_dependent`)
        is extended: revert calls fire at each
        mutation point. The orchestrator-contract
        scope stays the same; the new behavior is
        layered onto the existing fixture.

### 4. Real-git integration test

- [ ] Add a new case to
      `tests/test_swarm_git_integration.py`
      exercising the revert + fetch sequence against
      real git:
      1. `_make_bare_and_clone` builds the standard
         fixture; commit a `.beads/issues.jsonl` file
         at version V0 in the working clone and push.
         (Just a placeholder file with realistic
         shape; doesn't have to be a real bd export.)
      2. Locally modify `.beads/issues.jsonl` in the
         working clone (simulating bd's post-update
         hook).
      3. Push a new commit to bare remote that ALSO
         modifies `.beads/issues.jsonl` to a
         different value (simulating a worker commit
         captured in a PR merge).
      4. Call `GitAdapter().revert_paths(working_clone,
         (".beads/issues.jsonl",))`.
      5. Assert `git status --porcelain=v1` returns
         empty.
      6. Call `GitAdapter().fetch_and_ff_base(
         working_clone, "main")`.
      7. Assert local main's HEAD matches origin's
         tip; working tree clean.
- [ ] This is the regression contract for the iter-2
      smoke finding: a tracked file gets dirty
      locally, gets reverted, fetch+merge proceeds
      cleanly. Real git, not mocks.

### 5. Docs + CHANGELOG amendment

- [ ] `docs/architecture.md` Execution section: add a
      short "bd-state ownership" subsection between
      the state-machine block and the authority-model
      block. Three sentences:
      - bd's dolt db (`.beads/embeddeddolt/`) is
        gitignored and is the source of truth for bd
        state. Tracked file `.beads/issues.jsonl` is
        a derived export.
      - Turma owns the working-tree state of
        `.beads/issues.jsonl` on main. After each
        Turma-initiated bd mutation, the orchestrator
        reverts the file via
        `GitAdapter.revert_paths`. The dolt db keeps
        the mutation; the export gets re-generated
        on the worker's commit hook in the worktree.
      - This keeps `fetch_and_ff_base`'s `merge
        --ff-only` step able to run cleanly between
        iterations.
- [ ] `CHANGELOG.md` `[Unreleased]/Fixed`: amend the
      prior arc's roll-up entry with one bullet
      naming the iter-2 dirty-tree finding, the
      orchestrator-side `revert_paths` fix, and the
      bd-state-ownership decision. Reference this
      arc.
- [ ] No README changes. The "Base-branch sync"
      subsection's user-facing prose is unaffected.
- [ ] **`docs/smoke-turma-run.md` Step 3a gains two
      `git status --short` regression checks** — these
      are the core regression contract for this arc and
      need to live in the runbook so the smoke is
      reproducible from docs alone, not from chat
      context.
      - **After iteration 1**, before the manual
        `gh pr merge` step:
        ```bash
        git status --short    # must print NOTHING
        ```
        Add an explanatory sentence: "Pre-fix this
        line printed `M .beads/issues.jsonl`. The
        `swarm-beads-state-merge-cleanliness` arc's
        revert-after-mutation contract guarantees the
        working tree returns to clean after each
        Turma-driven bd update; an operator seeing
        any output here means a regression of that
        contract."
      - **After iteration 2** completes (after the
        sweep + claim-B + worker + mark-B sequence):
        ```bash
        git status --short    # must print NOTHING
        ```
        Add an explanatory sentence: "This is the
        more stringent check — iter-2 fires more bd
        mutations than iter-1 (sweep close + claim
        + mark, vs. iter-1's claim + mark). Any
        output means at least one Turma callsite is
        missing its `_revert_beads_export` call."
      - Brief paragraph at the top of Step 3a flagging
        these checks: "Two `git status --short` calls
        in this step are the regression contract for
        the bd-state-merge-cleanliness arc. If either
        prints any output, the smoke FAILED even if
        the rest of the chained-flow output looks
        right."

### 6. Validation

- [ ] `uv run pytest` green. Current baseline before
      this arc: 541 tests (after
      `swarm-fetch-and-ff-base-correction`). Expected
      net delta: roughly +6 to +10 (revert_paths
      adapter tests, orchestrator wiring tests,
      ordering pin, real-git integration test).
- [ ] No new runtime deps in `pyproject.toml`. `git`
      already a prerequisite.
- [ ] Live re-run of the chained smoke against
      `khanhgithead/turma-run-smoke` (left unchecked
      until the operator walks the runbook end-to-end
      against the live scratch). Walk Step 3a:
      - Iteration 1: `turma run --feature smoke-XYZ
        --max-tasks 1` opens task A's PR.
        **Critical regression check**: `git status`
        in the scratch dir AFTER iter-1 returns clean
        — `.beads/issues.jsonl` is NOT modified in the
        working tree.
      - Verify task A is `in_progress` with
        `turma-pr:<N>` (read via `bd show`, which
        reads from dolt — the source of truth).
      - Manual `gh pr merge <N> --squash` (no
        `--delete-branch`).
      - Iteration 2: re-run. Expect the full
        chained-flow sequence
        (`fetch: origin/main → main` succeeds,
        reconcile skip, merge-advancement close,
        claim B, worker, mark) end-to-end.
        **Critical regression check**: `git status`
        AFTER iter-2 is clean (the sweep mutations'
        reverts fired).
      - Verify task A closed without `turma-pr:`
        residue; task B's worktree LADDER.txt has
        both lines; task B's PR opened cleanly.
- [ ] On smoke success: tick the manual-smoke `[ ]`
      box on this arc's tasks.md (Task 6) AND on
      `swarm-fetch-and-ff-base-correction` (Task 4)
      AND on `swarm-merge-advancement-stabilization`
      (Task 7). All three were waiting on this arc.
- [ ] On smoke failure: triage in place, name the
      gap, stop, wait for direction. Do not start
      another fix branch autonomously.

### 7. Release gate

- [ ] After smoke passes and tasks.md is updated, the
      three correction arcs
      (`swarm-merge-advancement-stabilization`,
      `swarm-fetch-and-ff-base-correction`,
      `swarm-beads-state-merge-cleanliness`) together
      with the original `swarm-merge-advancement-
      stabilization` arc satisfy the 0.3.0 release
      prerequisites. Cutting 0.3.0 is a separate
      operator-driven action (version bump, dating
      `[Unreleased]`, tag, push) on explicit go.
