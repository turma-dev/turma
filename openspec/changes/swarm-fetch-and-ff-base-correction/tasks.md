## Tasks

### 1. Adapter: flip to three-call symbolic-ref + fetch + merge --ff-only

- [ ] In `src/turma/swarm/git.py`, replace
      `fetch_and_ff_base`'s single colon-form subprocess
      with three subprocess.run calls in order:
      ```
      git -C <repo_root> symbolic-ref --short HEAD
      git -C <repo_root> fetch origin <base_branch>
      git -C <repo_root> merge --ff-only origin/<base_branch>
      ```
- [ ] Failure mapping at the call boundary:
      - symbolic-ref returns a branch name (stdout) that
        is NOT `<base_branch>` → typed
        `PlanningError("HEAD is on <current>; turma run
        must run from a working copy with <base_branch>
        checked out. cd into the repo's <base_branch>
        checkout and re-run.")`. Fetch + merge NOT invoked.
      - symbolic-ref non-zero exit (detached HEAD,
        missing repo, etc.) → typed
        `PlanningError("HEAD is detached or unreadable:
        <stderr>; turma run requires <base_branch>
        checked out.")`. Fetch + merge NOT invoked.
      - Fetch non-zero exit → `PlanningError("git fetch
        failed: exit <N>\n<stderr>")`. Merge NOT invoked.
      - Merge non-zero exit with stderr containing
        `Not possible to fast-forward` or
        `non-fast-forward` → typed
        `PlanningError("local <base_branch> has diverged
        from origin/<base_branch>; refusing to
        fast-forward. Triage with `git log
        <base_branch>..origin/<base_branch>` and the
        reverse.")`
      - Merge non-zero exit, other → `PlanningError("git
        merge --ff-only failed: exit <N>\n<stderr>")`.
- [ ] Update the method's docstring to spell out the
      three-call argv, the HEAD-precheck behavior (refuses
      with explicit `cd` instruction when HEAD is on
      anything other than `<base_branch>`), and the
      five-way error mapping. Cross-reference
      `swarm-fetch-and-ff-base-correction/design.md`
      "`Adapter contract`" and "Why the HEAD precheck is
      in scope" subsections.
- [ ] Update existing subprocess-mock tests in
      `tests/test_swarm_git.py`:
      - `test_fetch_and_ff_base_pins_argv_shape`: assert
        THREE `subprocess.run` calls in order (symbolic-
        ref, fetch, merge) with the new argvs.
      - `test_fetch_and_ff_base_happy_path_returns_none`:
        all three calls return zero exit (symbolic-ref
        stdout = `<base_branch>`); method returns None.
      - `test_fetch_and_ff_base_typed_error_on_non_fast_forward`:
        symbolic-ref + fetch return 0; merge returns
        non-zero with `Not possible to fast-forward` in
        stderr → typed divergence error.
      - `test_fetch_and_ff_base_typed_error_on_rejected_substring`:
        **delete**. The colon-form's `[rejected]` phrasing
        no longer applies; merge --ff-only does not emit
        that string.
      - `test_fetch_and_ff_base_generic_error_preserves_stderr`:
        split into a fetch-network-failure case (assert
        merge never runs) and a merge-generic-failure
        case.
      - `test_fetch_and_ff_base_branch_name_interpolated_into_typed_error`:
        retained against merge-step stderr.
- [ ] Three new subprocess-mock tests:
      - `test_fetch_and_ff_base_typed_error_on_head_not_on_base`:
        symbolic-ref stdout is `feature-x` (not
        `<base_branch>`) → typed `cd`-instructing
        PlanningError; fetch + merge subprocess.run never
        called (assert exactly ONE call total).
      - `test_fetch_and_ff_base_typed_error_on_detached_head`:
        symbolic-ref non-zero exit with stderr `fatal:
        ref HEAD is not a symbolic ref` → typed detached-
        HEAD PlanningError; fetch + merge never called.
      - `test_fetch_and_ff_base_skips_merge_when_fetch_fails`:
        symbolic-ref ok; fetch raises non-zero → assert
        exactly TWO calls (symbolic-ref + fetch); merge
        not invoked.

### 2. Real-git integration test

- [ ] New file `tests/test_swarm_git_integration.py`. Shells
      out to the actual `git` binary against a tmpdir.
      Skip-if-git-missing guard at module level so the file
      is robust to environments without git (CI almost
      always has git; documented for completeness).
- [ ] Three tests:
      - **Happy path** (the case the live smoke caught):
        helper builds a tmpdir bare remote + a working
        clone with `main` checked out. A second working
        clone pushes a new commit to the bare remote.
        `fetch_and_ff_base(working_clone, "main")` runs
        against the first clone. Assert
        `git rev-parse HEAD` on the first clone now
        matches the bare remote's tip. This is the test
        that would have caught the colon-form bug — it
        exercises real git with HEAD on the destination
        ref.
      - **Divergent local**: bare remote at commit X,
        working clone at commit X, then a local commit Y
        on main (never pushed); meanwhile bare remote
        gets commit Z (via the second clone).
        `fetch_and_ff_base` raises typed `PlanningError`
        with "diverged" in the message and the branch
        name interpolated.
      - **HEAD on feature branch** (the silent-corruption
        case the precheck closes): working clone, check
        out a new branch off main (no commits required —
        the ancestor case is what would silently FF
        without the precheck). `fetch_and_ff_base("main")`
        raises typed `PlanningError` naming the current
        branch and pointing the operator at `cd`.
        Critically: assert the feature branch ref is
        unchanged after the failed call (compare
        `git rev-parse <feature>` before and after).
        That's the safety the precheck buys us; the
        assertion makes it a regression contract.
- [ ] Helpers shared across the three tests: a small
      `_make_bare_and_clone(tmp_path) -> tuple[Path, Path]`
      that returns `(bare_remote_path, working_clone_path)`
      with main initialized to a single committed file. Use
      `subprocess.run` directly (no `GitAdapter` shortcut)
      so the helpers are independent of the code under
      test. Keep the file under ~150 lines.

### 3. Docs + CHANGELOG amendment

- [ ] `docs/architecture.md` Execution section:
      replace the `fetch_and_ff_base` paragraph's "single-
      call colon-form" wording with "three-call
      `symbolic-ref` + `fetch` + `merge --ff-only`". Add
      sentences naming the colon-form's checkout-
      protection rejection as the reason for the
      correction AND the silent-feature-FF footgun the
      precheck closes.
- [ ] `CHANGELOG.md` `[Unreleased]/Fixed`: amend the prior
      arc's Finding 3 paragraph to name the three-call
      form (HEAD precheck + fetch + merge --ff-only) and
      reference this correction arc. Add one sentence
      about the live-smoke discovery and one about the
      silent-feature-FF footgun the precheck prevents so
      the changelog audit trail captures why the
      implementation flipped between versions.
- [ ] No README changes required. The "Base-branch sync"
      subsection's user-facing description (HEAD must be
      on the base branch, fetch fails loudly on
      divergence, --dry-run skips it) is correct as
      written.

### 4. Validation

- [ ] `uv run pytest` green. Current baseline: 536 tests
      (after `swarm-merge-advancement-stabilization`).
      Expected net delta: around +6 to +8 (one mock test
      deleted, three mock tests added for HEAD-precheck
      cases + the fetch-skip-on-failure ordering test,
      one mock test split into two, three integration
      tests added).
- [ ] No new runtime deps in `pyproject.toml`. `git`
      already a prerequisite.
- [ ] Live re-run of the chained smoke against
      `khanhgithead/turma-run-smoke`, walking
      `docs/smoke-turma-run.md` Step 3a end-to-end:
      - Iteration 1: `turma run --feature smoke-chain
        --max-tasks 1` opens task A's PR. Verify task A
        is `in_progress` with `turma-pr:<N>`.
      - Manual `gh pr merge <N> --squash` (no
        `--delete-branch`).
      - Iteration 2: `turma run --feature smoke-chain
        --max-tasks 1` should now print
        `fetch: origin/main → main`, `reconcile:   <id>
        → skipped (merge-tracked at PR #<N>)`,
        `merge-advancement: <id> → MERGED, closed`,
        then claim and run task B. Worker sees
        CHAINED.txt and appends.
      - Verify task A closed without `turma-pr:`
        residue; task B's worktree CHAINED.txt has both
        lines.
- [ ] On smoke success: tick the manual-smoke `[ ]` box
      on
      `openspec/changes/swarm-merge-advancement-stabilization/
      tasks.md` (Task 7's last unchecked box) as a
      follow-up commit.
- [ ] On smoke failure: triage in place, name the gap,
      stop, wait for direction. Do not start a fix branch
      autonomously.

### 5. Release gate

- [ ] After smoke passes and tasks.md is updated, the
      `swarm-merge-advancement-stabilization` arc and
      this correction arc together satisfy the 0.3.0
      release prerequisites. Cutting 0.3.0 is a separate
      action (version bump, dating
      `[Unreleased]`, tag, push) on explicit operator
      go.
