## Tasks

### 0. Investigate the wrong-path bd commit (DONE)

**Completed 2026-04-26b during the smoke triage.** The
shell-only reproducer below pins the upstream bd defect;
no agent is involved. The defect is filed as
[steveyegge/beads#3311](https://github.com/steveyegge/beads/issues/3311)
and fixed in bd 1.0.3 (release 2026-04-24, fix PR `#3347`:
"scrub git hook env and skip cross-worktree git-add").
Turma's commit-boundary fix is the local workaround that
keeps `turma run` correct on bd 1.0.2 (still on Homebrew at
the time of this writing) AND stays harmless on 1.0.3+.

The reproducer (run from any git repo with bd initialized
and `.beads/issues.jsonl` tracked at HEAD):

```bash
git worktree add -b probe .worktrees/probe HEAD
cd .worktrees/probe
bd prime                                    # silently deletes .beads/issues.jsonl
echo "stage one complete" > STAGE.txt
git add -A
git commit -m "[impl] reproducer"
git show --stat HEAD                         # observe wrong shape
```

The resulting commit's tree contains `issues.jsonl` at
the repo root AND a deletion of `.beads/issues.jsonl` —
the buggy shape this arc's commit-boundary protocol works
around. Tasks 1-3 below close it; Task 5 carries the
prepared upstream bug report for filing.

- [x] Static probe of Turma's worker contract (worker.py,
      worktree.py, ClaudeCodeWorker.run, render_worker_prompt):
      no bd command in the prompt, no env injection, no
      helper script around the worker. Cause is NOT in
      Turma's worker invocation.
- [x] Instrumented Claude Code run with
      `--output-format stream-json --verbose
      --include-hook-events`: agent makes only Write tool
      calls. No Bash, no bd commands, no git operations.
      Cause is NOT in the agent.
- [x] Plain bd inside a registered worktree (no `bd prime`):
      writes to `.beads/issues.jsonl` correctly. Cause is
      NOT in bd's worktree-handling generally.
- [x] Plain shell sequence (`git worktree add` → `bd prime`
      → write file → `git add -A` → `git commit`):
      reproduces the wrong-shaped commit exactly. Cause is
      `bd prime` + bd's pre-commit hook combined.
- [x] `export.interval=0` independently fixes Finding 1
      (read-side dirtying). Verified: write → revert →
      multiple bd reads → file stays clean.

### 1. Preflight: refuse on `export.interval` ≠ 0

- [ ] In `src/turma/swarm/_orchestrator.py`, add
      `_preflight_bd_export_interval(services)` helper
      that runs `bd config get export.interval` (or the
      equivalent config-read command) via a new
      `BeadsAdapter.config_get(key)` method. Refuses with
      a typed `PlanningError` if the value is anything
      other than `"0"`.
- [ ] The error message names the key, the expected value,
      the rationale, and the exact remediation:
      ```
      turma run requires `bd config get export.interval`
      to return 0 (default is 60). The 60s throttle
      defers bd's auto-export across iterations; with
      the throttle in place, any bd command between
      turma runs (including reads like `bd list --json`)
      re-dirties .beads/issues.jsonl and the next
      preflight refuses.
      
      Run:
        bd config set export.interval 0
      
      The setting persists in .beads/config.yaml.
      ```
- [ ] Call site in `run_swarm`: invoke
      `_preflight_bd_export_interval(services)` after the
      existing `_preflight(...)` and BEFORE
      `_preflight_beads_state_clean(services)`. **Skipped
      under `--dry-run`** consistent with the other
      preflights.
- [ ] In `src/turma/swarm/beads.py` (or wherever the
      BeadsAdapter lives), add `config_get(key: str) ->
      str`. argv pinned:
      ```
      bd config get <key>
      ```
      Returns stdout stripped. Failure → `PlanningError(
      "bd config get failed: exit <N>\n<stderr>")`.
- [ ] Tests in `tests/test_swarm_run.py`:
      - New `test_run_swarm_refuses_when_export_interval_nonzero`:
        stub `BeadsAdapter.config_get("export.interval")`
        returns `"60"` → run_swarm raises typed
        PlanningError with the expected-value message and
        the remediation command. No further phases run.
      - New `test_run_swarm_proceeds_when_export_interval_zero`:
        stub returns `"0"` → run_swarm proceeds past this
        preflight (existing happy-path test trivially
        extends with the stub default).
      - New `test_dry_run_skips_export_interval_preflight`:
        stub returns `"60"` → dry-run completes without
        raising.
- [ ] Tests in `tests/test_swarm_beads_extensions.py` (or
      wherever BeadsAdapter argv is pinned): argv-pin for
      `config_get`; failure-surfaces test.

### 2. Worker-commit boundary: explicit bd export + hook bypass

- [ ] In `src/turma/swarm/git.py`, modify `commit_all` to
      take an optional pre-stage callback. Or simpler: add
      a new `commit_all_with_bd_export(worktree, message,
      *, services)` method that owns the four-step
      sequence:
      1. `services.beads.export(output_path=worktree /
         ".beads" / "issues.jsonl")` — runs `bd export
         -o <abs-path>` from `services.repo_root` cwd.
      2. (existing dirty-status check)
      3. `git -C <worktree> add -A`
      4. `git -C <worktree> -c core.hooksPath=/dev/null
         commit -m <message>`
      
      Returning the commit SHA, same as today.
- [ ] In `src/turma/swarm/beads.py`, add
      `BeadsAdapter.export(output_path: Path) -> None`.
      argv pinned:
      ```
      bd export -o <output_path>
      ```
      cwd: `self._repo_root` (NOT the worktree).
      Failure → `PlanningError("bd export failed: exit <N>
      \n<stderr>")`.
- [ ] Switch the orchestrator's worker-commit call site
      (in `_run_single_task`, after `worker.run` returns
      success) to use the new `commit_all_with_bd_export`
      method instead of plain `commit_all`. The plain
      `commit_all` stays callable for any non-bd-aware
      use case (today there is none in `turma run`'s
      worker flow, but keeping the method doesn't cost
      anything).
- [ ] Unit tests for `commit_all_with_bd_export` in
      `tests/test_swarm_git.py`:
      - argv-pin for the four subprocess calls in order
        (bd export, git add -A, git commit with
        `-c core.hooksPath=/dev/null`).
      - The bd export call uses `services.repo_root` as
        cwd, NOT the worktree.
      - The bd export call's `-o` argument is the
        worktree's `.beads/issues.jsonl` absolute path.
      - The git commit call's argv contains `-c
        core.hooksPath=/dev/null` (regression contract:
        future "simplification" attempts that drop the
        flag re-introduce Finding 2).
      - Empty `git add -A` → status_is_dirty refusal (the
        existing empty-commit guard still applies).
      - **Failure-boundary contract — no partial commits.**
        When bd export raises, no `git add -A` and no
        `git commit` subprocess fires (assert via the
        stub recorder's call list). Same for the
        post-export "destination missing" check: when
        the destination assertion fails, no `git add` /
        `git commit` follows. The protocol is binary —
        either all four steps run or zero git mutations
        do.
      - bd export failure → PlanningError surfaces stderr.
      - bd export reports zero exit but destination file
        does not exist → PlanningError("bd export reported
        success but destination path is missing: <path>");
        no commit follows.
      - git commit failure → PlanningError surfaces stderr.
- [ ] Unit tests for `BeadsAdapter.export` in
      `tests/test_swarm_beads_extensions.py`:
      - argv-pin: `bd export -o <abs-path>`.
      - cwd-pin: respects the adapter's repo_root.
      - failure-surfaces.

### 3. Real-git integration test: the reproducer shape

The buggy-shape assumption lives in **exactly one** dedicated
integration test (the negative control below). Other tests in
this arc — including the happy-path integration test and all
unit tests — must NOT encode the buggy shape; they assert
correctness, not the absence of bugness in upstream bd. This
isolation makes the eventual upstream-bd-fix observable in
exactly one place when it ships.

- [ ] Happy-path test:
      `test_commit_all_with_bd_export_against_real_git_and_real_bd`.
      Skipif `bd` is not on PATH (alongside the existing
      `git`-skipif).
      
      The test mirrors the Task 0 reproducer above:
      
      1. Build a tmpdir bare remote + working clone with
         a `.beads/issues.jsonl` at HEAD (use
         `BD_NON_INTERACTIVE=1 bd init` against the clone
         and commit with `core.hooksPath=/dev/null` to
         avoid the documented `bd init` hang on macOS).
      2. Create a registered worktree via plain
         `git worktree add`.
      3. Run `bd prime` inside the worktree.
      4. Write a non-bd file inside the worktree
         (`echo > STAGE.txt`).
      5. Call
         `GitAdapter().commit_all_with_bd_export(...)`
         with the appropriate services.
      6. Assert the resulting commit:
         - touches `STAGE.txt` (added)
         - touches `.beads/issues.jsonl` (added or
           modified, depending on whether HEAD had it)
         - does NOT add `issues.jsonl` at root
         - the worktree's `.beads/issues.jsonl` content
           matches `bd export` from main's repo root at
           commit time (canonical state propagation
           preserved)
- [ ] **Negative control test** (the SOLE place that
      asserts the upstream buggy shape):
      `test_plain_commit_after_bd_prime_reproduces_upstream_bd_bug`.
      Same setup as the happy-path test, but uses plain
      `git -C <worktree> add -A && git commit` (no hook
      bypass, no explicit export). Asserts the BUGGY shape:
      root `issues.jsonl` added AND `.beads/issues.jsonl`
      deleted.
      
      Includes an inline test docstring that says verbatim:
      
      > "If this test starts failing, upstream bd has
      > likely fixed the pre-commit hook path-resolution
      > defect this workaround was written for. See
      > `openspec/changes/swarm-worker-commit-bd-
      > ownership/design.md` and re-evaluate whether the
      > hook bypass in `commit_all_with_bd_export` is
      > still needed. Do NOT silence this test — read the
      > triage chain and consider removing or simplifying
      > the workaround."
      
      The docstring is the single source of truth on
      what an unexpected pass means. No other test
      mentions the buggy shape. No other test gives
      removal guidance.

### 4. Docs + CHANGELOG amendment

- [ ] `docs/architecture.md` "bd-state ownership" section
      (added by the prior arc): append a short paragraph
      documenting the worker-commit boundary contract. New
      sentences:
      - At the worker-commit boundary, Turma runs
        `bd export -o <worktree>/.beads/issues.jsonl` from
        main's repo root, then commits with
        `core.hooksPath=/dev/null`. This sidesteps a bd
        upstream defect where bd's pre-commit hook,
        fired against an index containing `D .beads/
        issues.jsonl` (the state `bd prime` itself
        creates), misroutes the export to the worktree's
        repo root. Turma's explicit export at the
        canonical path keeps worker-commit propagation
        intact. See
        `openspec/changes/swarm-worker-commit-bd-ownership/`
        for the full contract and the no-agent shell-only
        reproducer that pins the upstream defect.
- [ ] `CHANGELOG.md` `[Unreleased]/Fixed`: amend with one
      bullet naming the wrong-path worker-commit defect,
      Turma's commit-boundary ownership response, and the
      `export.interval=0` preflight contract.
- [ ] `docs/smoke-turma-run.md` Prerequisites: add the
      `bd config set export.interval 0` step to the bd
      init flow, with a one-line rationale referencing this
      arc.
- [ ] `docs/smoke-turma-run.md` Step 3a: add a third
      regression check after the existing two:
      ```bash
      git show --stat HEAD       # latest worker commit
      ```
      Explanatory paragraph: "Critical regression check
      for `swarm-worker-commit-bd-ownership`: the worker
      commit must NOT contain `issues.jsonl` at the repo
      root, and `.beads/issues.jsonl` must NOT be
      deleted. Either condition means the bd hook bypass
      regressed and Turma is generating bd-state-corrupt
      worker commits again."
- [ ] No README changes. The "Base-branch sync" and "Swarm
      Execution" sections' user-facing prose are
      unaffected.

### 5. Upstream filing (no-op — already filed and fixed)

**Completed 2026-04-26 during the verification walk.** The
upstream defect was already filed as
[steveyegge/beads#3311](https://github.com/steveyegge/beads/issues/3311)
by `jacob-ablowitz` (closed 2026-04-21) and fixed in bd
1.0.3 (released 2026-04-24, fix PR `#3347`). Confirmed
empirically against a downloaded 1.0.3 binary: the
no-agent shell reproducer no longer produces the buggy
shape under 1.0.3. Filing a duplicate would be
counterproductive; this arc references bd#3311 directly.

- [x] Verify the prepared reproducer against the latest bd
      release before filing → 1.0.3 fixes the wrong-path
      defect (verified 2026-04-26 with the GitHub-release
      binary at `/tmp/bd-1.0.3-probe/bd`).
- [x] Don't file a duplicate. bd#3311 already covers it.
- [x] Link the upstream issue URL into this arc's
      `proposal.md` "Key insight" section (done in the
      same commit that lands this checkbox tick).
- [ ] **Follow-up — version sensitivity of the
      negative-control test.** Once bd 1.0.3 lands in the
      operator's PATH (currently still on Homebrew 1.0.2
      at the time of this writing), the
      `test_plain_commit_after_bd_prime_reproduces_upstream_bd_bug`
      test in `tests/test_swarm_git_integration.py` will
      start failing because the buggy shape no longer
      reproduces. That is the signal the test was
      designed to surface; the docstring already tells
      future readers what to do. The decision of how to
      RESHAPE that test (skipif on bd version? convert to
      a regression-on-fixed-version? remove?) is an
      explicit follow-up outside this arc — the
      maintainer who runs the smoke against 1.0.3+ should
      decide based on whether the protocol is still
      load-bearing for non-bug reasons (it is: bd 1.0.3
      deliberately skips cross-worktree git-add, so
      Turma's explicit export still ensures bd state
      lands in worker commits).

### 6. Validation

- [ ] `uv run pytest` green. Baseline before this arc:
      562 tests. Expected delta: roughly +10 to +15.
- [ ] No new runtime deps in `pyproject.toml`. `bd` is
      already a prerequisite.
- [ ] Re-run the chained-flow smoke against
      `khanhgithead/turma-run-smoke-2` (or a fresh
      disposable repo). Walk Step 3a end-to-end.
      **Critical regression checks** in addition to the
      two from the prior arc:
      - After iter-1: `git show --stat origin/task/<feat>/
        <id>` does NOT contain `issues.jsonl` at root and
        `.beads/issues.jsonl` is NOT deleted.
      - After iter-2: same check on iter-2's worker
        commit.
      - After both iters: `git status --short` is empty
        (the prior arc's contract still holds).
- [ ] On smoke success: tick all manual-smoke `[ ]` boxes
      across the four correction arcs
      (`swarm-merge-advancement-stabilization`,
      `swarm-fetch-and-ff-base-correction`,
      `swarm-beads-state-merge-cleanliness`, this arc).
      The prior three were waiting on
      `swarm-beads-state-merge-cleanliness`; this arc
      gates them all.
- [ ] On smoke failure: triage in place, name the gap,
      stop, wait for direction. Do not start another fix
      branch autonomously.

### 7. Release gate

- [ ] After smoke passes and tasks.md is updated, the
      four correction arcs together with the original
      `swarm-orchestration` arc satisfy the 0.3.0 release
      prerequisites. Cutting 0.3.0 is a separate
      operator-driven action (version bump, dating
      `[Unreleased]`, tag, push) on explicit go.
