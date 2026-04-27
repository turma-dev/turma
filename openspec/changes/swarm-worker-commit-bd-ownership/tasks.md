## Tasks

### 0. Investigate the wrong-path bd commit (DONE)

**Completed 2026-04-26b during the smoke triage.** Pinned in
`docs/upstream-bd-worktree-precommit-bug.md` (planning
repo). Reproducer is shell-only, no agent involved. The
defect is upstream bd; Turma's commit-boundary fix is the
local workaround.

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
      - bd export failure → PlanningError surfaces stderr.
      - git commit failure → PlanningError surfaces stderr.
- [ ] Unit tests for `BeadsAdapter.export` in
      `tests/test_swarm_beads_extensions.py`:
      - argv-pin: `bd export -o <abs-path>`.
      - cwd-pin: respects the adapter's repo_root.
      - failure-surfaces.

### 3. Real-git integration test: the reproducer shape

- [ ] Add a new case to
      `tests/test_swarm_git_integration.py`:
      `test_commit_all_with_bd_export_against_real_git_and_real_bd`.
      Skipif `bd` is not on PATH (alongside the existing
      `git`-skipif).
      
      The test mirrors the
      `docs/upstream-bd-worktree-precommit-bug.md`
      reproducer:
      
      1. Build a tmpdir bare remote + working clone with
         a `.beads/issues.jsonl` at HEAD (use
         `BD_NON_INTERACTIVE=1 bd init` against the clone
         and commit with `core.hooksPath=/dev/null` to
         avoid the documented `bd init` hang on macOS).
      2. Create a registered worktree via plain
         `git worktree add`.
      3. Run `bd prime` inside the worktree (pin: this
         step deletes the worktree's
         `.beads/issues.jsonl` from the working tree —
         assert the deletion explicitly so a future bd
         release that fixes this surfaces the assertion
         failure).
      4. Write a non-bd file inside the worktree
         (`echo > STAGE.txt`).
      5. Call
         `GitAdapter().commit_all_with_bd_export(...)`
         with the appropriate services.
      6. Assert the resulting commit:
         - touches `STAGE.txt` (added)
         - touches `.beads/issues.jsonl` (added or
           modified, depending on whether HEAD had it)
         - does NOT add `issues.jsonl` at root (the
           bug shape) — explicit assertion against the
           tree at the new commit
         - the worktree's `.beads/issues.jsonl` content
           matches `bd export` from main's repo root at
           commit time (canonical state propagation
           preserved)
- [ ] Negative control test in the same file:
      `test_plain_commit_after_bd_prime_reproduces_bug_shape`.
      Same setup but uses plain `git -C <worktree>
      add -A && git commit` (no hook bypass, no explicit
      export). Asserts the BUGGY shape (root `issues.jsonl`
      added, `.beads/issues.jsonl` deleted). This pins the
      reproducer so a future bd release that fixes the
      defect surfaces an unexpected pass — at which point
      this arc's hook-bypass becomes optional.

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
        for the contract; the upstream defect is
        documented in the planning repo at
        `docs/upstream-bd-worktree-precommit-bug.md`.
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

### 5. File the upstream bd issue (operator-driven)

- [ ] Walk
      `docs/upstream-bd-worktree-precommit-bug.md` against
      the latest bd release before filing in case 1.0.3+
      ships a fix.
- [ ] If still reproducible: file at the bd upstream
      tracker. This is operator-driven; not in scope of
      this PR.
- [ ] On filing, link the upstream issue URL into this
      arc's `proposal.md` "Key insight" section so future
      readers can cross-reference.

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
