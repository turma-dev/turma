## Tasks

### 1. Adapter: flip to two-call fetch + merge --ff-only

- [ ] In `src/turma/swarm/git.py`, replace
      `fetch_and_ff_base`'s single colon-form subprocess
      with two subprocess.run calls in order:
      ```
      git -C <repo_root> fetch origin <base_branch>
      git -C <repo_root> merge --ff-only origin/<base_branch>
      ```
- [ ] Failure mapping at the call boundary:
      - Fetch non-zero exit → `PlanningError("git fetch
        failed: exit <N>\n<stderr>")`. Merge step is NOT
        invoked.
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
- [ ] HEAD-on-`<base_branch>` is documented as a
      precondition in the docstring; v1 does NOT
      explicitly check it. See `design.md`
      "HEAD-on-<base_branch> is a documented
      precondition, not a checked one" for why
      (`git merge --ff-only` has no clean detection
      signal for HEAD-on-feature; would require a third
      subprocess call to `git symbolic-ref`, deferred).
- [ ] Update the method's docstring to spell out the
      two-call argv, the HEAD-on-`<base_branch>`
      precondition (with explicit "v1 does not check"
      caveat), and the three-way error mapping. Cross-
      reference `swarm-fetch-and-ff-base-correction/
      design.md` "`Adapter contract`" subsection.
- [ ] Update existing subprocess-mock tests in
      `tests/test_swarm_git.py`:
      - `test_fetch_and_ff_base_pins_argv_shape`: assert
        TWO `subprocess.run` calls in order (fetch then
        merge) with the new argvs.
      - `test_fetch_and_ff_base_happy_path_returns_none`:
        both calls return zero exit; method returns None.
      - `test_fetch_and_ff_base_typed_error_on_non_fast_forward`:
        fetch returns 0; merge returns non-zero with
        `Not possible to fast-forward` in stderr →
        typed divergence error.
      - `test_fetch_and_ff_base_typed_error_on_rejected_substring`:
        **delete**. The colon-form's `[rejected]` phrasing
        no longer applies; merge --ff-only does not emit
        that string.
      - `test_fetch_and_ff_base_generic_error_preserves_stderr`:
        split into a fetch-network-failure case (merge
        never runs — assert fetch was the only call) and
        a merge-generic-failure case.
      - `test_fetch_and_ff_base_branch_name_interpolated_into_typed_error`:
        retained against merge-step stderr.
- [ ] One new subprocess-mock test:
      - `test_fetch_and_ff_base_skips_merge_when_fetch_fails`:
        fetch raises non-zero → assert exactly ONE
        `subprocess.run` call. Pin the ordering.

      The earlier draft of this task added a second new
      test for HEAD-not-on-base detection via
      `merge: <base> - not something we can merge` stderr
      — that test is dropped because git's actual behavior
      doesn't emit that signal for HEAD-on-feature (it
      either silently FFs the feature ref or surfaces the
      same `Not possible to fast-forward` signal as
      divergence). v1 documents HEAD-on-`<base_branch>`
      as an unchecked precondition. See `design.md`
      "HEAD-on-<base_branch> is a documented precondition,
      not a checked one" for the rationale.

### 2. Real-git integration test

- [ ] New file `tests/test_swarm_git_integration.py`. Shells
      out to the actual `git` binary against a tmpdir.
      Skip-if-git-missing guard at module level so the file
      is robust to environments without git (CI almost
      always has git; documented for completeness).
- [ ] Two tests:
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

      A HEAD-on-feature-branch test was originally
      planned here too. It's dropped because git's
      actual behavior in that case is split (silent FF
      of the feature ref vs. divergence error), neither
      of which gives a clean "HEAD not on base"
      assertion to write — see Task 1's note on the
      same. Validating that v1 does NOT silently corrupt
      a feature branch when HEAD is on it would require
      either checking the feature ref unchanged (an
      indirect test) or adding the deferred
      `git symbolic-ref` precheck (out of scope for this
      arc).
- [ ] Helpers shared across the two tests: a small
      `_make_bare_and_clone(tmp_path) -> tuple[Path, Path]`
      that returns `(bare_remote_path, working_clone_path)`
      with main initialized to a single committed file. Use
      `subprocess.run` directly (no `GitAdapter` shortcut)
      so the helpers are independent of the code under
      test. Keep the file under ~150 lines.

### 3. Docs + CHANGELOG amendment

- [ ] `docs/architecture.md` Execution section:
      replace the `fetch_and_ff_base` paragraph's "single-
      call colon-form" wording with "two-call
      `fetch + merge --ff-only`". One additional sentence
      naming the colon-form's checkout-protection
      rejection as the reason for the correction.
- [ ] `CHANGELOG.md` `[Unreleased]/Fixed`: amend the prior
      arc's Finding 3 paragraph to name the two-call form
      and reference this correction arc. Add one sentence
      about the live-smoke discovery so the changelog
      audit trail captures why the implementation flipped
      between versions.
- [ ] No README changes required. The "Base-branch sync"
      subsection's user-facing description (HEAD must be
      on the base branch, fetch fails loudly on
      divergence, --dry-run skips it) is correct as
      written.

### 4. Validation

- [ ] `uv run pytest` green. Current baseline: 536 tests
      (after `swarm-merge-advancement-stabilization`).
      Expected net delta: roughly 0 to +2 (one mock test
      deleted, two mock tests added, one mock test split
      into two, three integration tests added; nets to
      around +5 minus the deletion).
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
