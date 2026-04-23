## Tasks

### 1. Extend BeadsAdapter with ready / claim / fail / retries

- [ ] **Determine the argv for each new operation first.** Run
      `bd --help`, `bd list --help`, and whatever bd subcommands
      handle state transitions and label management (candidates:
      `bd ready`, `bd start`, `bd set-state`, `bd assign`,
      `bd label`, `bd note`), then pick the minimal argv that yields
      the needed semantics for each adapter method. Document the
      chosen commands in the `BeadsAdapter` class docstring with the
      same argv-pinning test pattern used in Task 2 of
      `beads-transcription`.
- [ ] Add `list_ready_tasks(feature: str) -> tuple[BeadsTaskRef, ...]`
      returning OPEN + `feature:<name>` + unblocked tasks, AND
      filtering out any task carrying the `needs_human_review` label
      so retry-exhausted tasks do not appear as ready.
- [ ] Add `claim_task(task_id: str) -> None` that atomically
      transitions the task to `in_progress`. Raises `PlanningError`
      with `bd` stderr if the task is already claimed, closed, or
      still blocked.
- [ ] Add `retries_so_far(task_id: str) -> int` that reads the
      `turma-retries:<n>` label off the task (0 if absent). The
      orchestrator owns budget accounting and calls this before
      invoking `fail_task`.
- [ ] Add `fail_task(task_id, reason, *, retries_so_far,
      max_retries) -> None`. Records the reason via `bd note`,
      updates the `turma-retries:<n>` label, and either releases the
      claim back to `open` (budget remaining) or adds
      `needs_human_review` + releases (budget exhausted). Idempotent
      — safe to retry the same call on adapter-level transient
      failure.
- [ ] `close_task` already exists from `beads-transcription`; add a
      unit test documenting that it remains usable by the swarm
      orchestrator on worker success.
- [ ] Full unit coverage in `tests/test_swarm_beads_extensions.py`:
      argv pinned for each new method; happy path; claim-race
      rejection surfaces `bd` stderr; `list_ready_tasks` excludes
      `needs_human_review`-labelled tasks; `fail_task` adds vs
      removes the retry label at the correct budget boundary;
      `retries_so_far` reads the label from `bd list --json` output
      structure.

### 2. Add the WorktreeManager

- [ ] New module `src/turma/swarm/worktree.py`.
- [ ] `WorktreeManager(repo_root: Path, worktree_root: str)` with
      methods:
      - `setup(feature, task_id, base_branch) -> Path`: creates
        `./<worktree_root>/<feature>/<task_id>/` on branch
        `task/<feature>/<task_id>` based on `<base_branch>`, or
        returns the existing path if the worktree is already
        registered with git.
      - `cleanup(path: Path) -> None`: `git worktree remove` +
        `git branch -D` for the associated task branch. Called only
        on task success.
- [ ] argv pinned: `git worktree list --porcelain` for
      existing-worktree detection, `git worktree add <path> -b
      <branch> <base>` for creation, `git worktree remove --force
      <path>` + `git branch -D <branch>` for cleanup.
- [ ] Branch collision (existing `task/<feature>/<task_id>` branch
      without a worktree) raises `PlanningError` with the `git`
      stderr preserved.
- [ ] `.gitignore`: add `.worktrees/`.
- [ ] Unit tests in `tests/test_swarm_worktree.py`: create new,
      reuse existing, collision rejection, cleanup happy path,
      cleanup when the branch is missing.

### 3. Add the WorkerBackend protocol and ClaudeCodeWorker

- [ ] New module `src/turma/swarm/worker.py` exposing
      `WorkerInvocation`, `WorkerResult`, and the `WorkerBackend`
      Protocol.
- [ ] `ClaudeCodeWorker` implementation:
      - `__init__` validates `shutil.which("claude")` and raises
        `PlanningError` with an install hint on failure.
      - `run(invocation)` renders the pinned prompt (see design doc
        "Worker backend protocol" section), invokes
        `claude -p <prompt> --cwd <worktree>
        --dangerously-skip-permissions` via `subprocess.run` with
        `timeout=invocation.timeout_seconds`, and inspects the
        worktree for sentinels to derive the `WorkerResult`.
      - Sentinel precedence: `.task_complete` → success;
        `.task_failed` → failure with file contents as reason;
        neither → failure with reason `"worker exited without
        writing a completion marker"`. Timeout → status `"timeout"`.
- [ ] Worker registry: `_BACKENDS: dict[str, Callable[[],
      WorkerBackend]]` keyed by name. v1 registers only
      `"claude-code"`.
- [ ] `.gitignore`: add `.task_complete`, `.task_progress`,
      `.task_failed`.
- [ ] Unit tests in `tests/test_swarm_worker.py`: prompt renders
      with the expected fields, each sentinel-detection branch,
      timeout path, missing-CLI path, unknown-backend-name path.

### 4. Add the GitAdapter

- [ ] New module `src/turma/swarm/git.py` exposing `GitAdapter` with
      three operations on a worktree path: `status_is_dirty`,
      `commit_all(message)`, `push_branch(branch, remote='origin')`.
- [ ] `__init__` validates `shutil.which("git")` and raises
      `PlanningError` if git is missing.
- [ ] argv pinned (each verified with subprocess stubs in tests):
      - `git -C <worktree> status --porcelain=v1`
      - `git -C <worktree> add -A`
      - `git -C <worktree> commit -m <message>`
      - `git -C <worktree> rev-parse HEAD`
      - `git -C <worktree> push --set-upstream <remote> <branch>`
- [ ] `commit_all` refuses to create an empty commit: if
      `status_is_dirty` is False before committing, raise
      `PlanningError("nothing to commit")` with the orchestrator
      expected to convert that into a worker-failure path rather
      than attempting the commit.
- [ ] Non-zero exits on any git call raise `PlanningError` with
      git's stderr preserved verbatim (falling back to stdout if
      stderr is empty). Auth / non-fast-forward / network failures
      all surface unchanged.
- [ ] Commit-message template lives as a module-level constant that
      the orchestrator renders. Pinned shape:
      ```
      [{turma_type}] {task_title}

      Closes bd-{task_id}.

      Generated by turma run for feature "{feature}".
      ```
- [ ] Credentials are assumed to already work (ssh agent or
      `gh auth` credential helper). Adapter does not manage creds.
- [ ] Unit tests in `tests/test_swarm_git.py` with subprocess stubs:
      argv shape for each operation, dirty-tree detection (empty
      porcelain vs non-empty), SHA parse from rev-parse, empty
      commit rejection, push failure surfaces git stderr, missing
      CLI path.

### 5. Add the PullRequestAdapter

- [ ] New module `src/turma/swarm/pull_request.py` with
      `PullRequestAdapter.open_pr(*, branch, base, title, body) ->
      str`.
- [ ] `__init__` validates `shutil.which("gh")` and runs
      `gh auth status` once to fail early if the user's session is
      expired.
- [ ] argv pinned: `gh pr create --head <branch> --base <base>
      --title <title> --body <body>`. Parse the returned PR URL from
      stdout.
- [ ] Non-zero exit raises `PlanningError` with `gh` stderr
      preserved. No automatic retry in v1.
- [ ] Unit tests in `tests/test_swarm_pull_request.py` with
      subprocess stubs: argv shape, URL parsing, non-zero exit,
      missing CLI, unauthenticated session.

### 6. Add reconciliation

- [ ] New module `src/turma/swarm/reconciliation.py` with
      `reconcile_feature(feature, *, adapter, worktree_manager,
      git_adapter, repo_root) -> ReconciliationReport`.
- [ ] **Read-only.** The module detects ambiguity and returns a
      typed report; it never calls `fail_task`, `close_task`,
      `git commit`, `git push`, or `gh pr create`. Those mutations
      are performed by the orchestrator's repair phase (Task 7).
- [ ] `ReconciliationReport.findings` is an ordered tuple of typed
      dataclasses — `missing-worktree`, `completion-pending`,
      `completion-pending-with-pr`, `failure-pending`,
      `stale-no-sentinels`, `orphan-branch` — each naming the task
      id (when applicable) and the suggested repair action from the
      design doc's finding table.
- [ ] Print a short summary to stdout (`reconcile: N ...`) on
      module entry; the orchestrator prints per-repair lines
      separately as it applies them.
- [ ] Unit tests in `tests/test_swarm_reconciliation.py`: each
      finding category produces the expected typed entry and the
      module makes no mutation calls against any adapter (verified
      by asserting zero close/fail/push/PR calls on the stubs).

### 7. Wire the swarm orchestrator

- [ ] New module `src/turma/swarm/__init__.py` exposing
      `run_swarm(feature, *, services=None, max_tasks=None,
      backend=None, dry_run=False)`.
- [ ] Preflight: verify `openspec/changes/<feature>/`, `APPROVED`,
      `TRANSCRIBED.md`. Missing any → `PlanningError` with a pointer
      at `turma plan` or `turma plan-to-beads`.
- [ ] Call reconciliation (always, even on `--dry-run`). On
      `--dry-run`, print the report and exit without entering the
      repair phase or the main loop.
- [ ] **Repair phase (between reconciliation and fetch_ready).**
      Consumes `ReconciliationReport.findings` in order and applies
      the documented repair per finding type:
      - `missing-worktree` → release the claim via `fail_task`
        (treat as a failed attempt against the retry budget).
      - `completion-pending` → commit + push + open_pr + close_task.
      - `completion-pending-with-pr` → close_task, remove worktree.
      - `failure-pending` → fail_task with the worker's reason,
        remove worktree.
      - `stale-no-sentinels` → halt the run before fetch_ready; do
        NOT guess.
      - `orphan-branch` → log only; never touched in v1.
- [ ] Main loop: `fetch_ready → claim → setup_worktree →
      run_worker → (sentinel dispatch) → git_commit_push → open_pr
      → close_task OR fail_task → next`.
- [ ] On `run_worker` success + clean tree (status_is_dirty is
      False), treat as a worker failure with reason `"worker
      reported success but left the tree clean"` — enter the retry
      path.
- [ ] Budget enforcement: read `retries_so_far(task_id)` before
      `fail_task`; pass `max_retries` along so the adapter can add
      `needs_human_review` at exhaustion. Exhausted-budget failures
      halt the outer loop. `max_tasks` caps overall loop iterations
      (default unbounded).
- [ ] `SwarmServices` dataclass for dependency injection (Beads +
      Worktree + Git + PR adapters + worker factory), mirroring
      `PlanningServices` and the transcription shape. Tests inject
      stubs directly.
- [ ] Integration tests in `tests/test_swarm_run.py` using
      `StubBeadsAdapter`, `StubWorktreeManager`, `StubGitAdapter`,
      `StubWorkerBackend`, `StubPullRequestAdapter`. Cover: one-task
      happy loop; multi-task sequential loop; claim race (other run
      won the task); worker success path including git+push+PR;
      worker success with clean tree → retry path; worker fail
      with budget remaining → retry path; worker fail budget
      exhausted → halts outer loop; each reconciliation repair
      finding drives the expected adapter calls; `--dry-run` never
      calls any mutation; preflight failures.

### 8. Wire the CLI subcommand

- [ ] Replace the placeholder `turma run` in `src/turma/cli.py` with
      the real dispatch: `--feature` required; `--max-tasks`,
      `--backend`, `--dry-run` optional.
- [ ] Construct the default services (real `BeadsAdapter`,
      `WorktreeManager`, `GitAdapter`, `PullRequestAdapter`,
      registered worker backend) and call `run_swarm`.
- [ ] Map `PlanningError` to exit 1 with `error: <message>` on
      stdout, matching the pattern in `turma plan` and
      `turma plan-to-beads`.
- [ ] Print a compact per-task summary as the loop progresses
      (claim → PR URL → close), mirroring the resume / transcription
      output style.
- [ ] CLI tests in `tests/test_swarm_cli.py`: subparser registered,
      `--feature` required, unknown-flag rejection, happy path calls
      through `run_swarm` with the parsed args, `PlanningError`
      becomes exit 1.

### 9. Docs, config, and live validation

- [ ] Update `turma.example.toml` with the documented `[swarm]`
      block: `worker_backend`, `worker_timeout`, `max_retries`,
      `worktree_root`, `base_branch` (all with v1 defaults).
- [ ] Update `README.md` with a new "Swarm Execution" section:
      prerequisites (`bd`, `gh`, Claude Code CLI), the one-feature
      loop, failure modes, the reconciliation-on-resume behavior,
      and a worked example against a small transcribed feature.
- [ ] Update `docs/architecture.md` Execution section from its
      current high-level framing to the committed v1 contract.
- [ ] New `docs/smoke-turma-run.md` runbook — same shape as
      `docs/smoke-plan-to-beads.md`: prerequisites, scratch setup,
      happy-path validation commands, failure-mode cheat sheet.
      Runs against a real `bd` + `gh` + `claude` install.
- [ ] `CHANGELOG.md` `[Unreleased]` entry rolling up the change
      set.
- [ ] No new runtime deps in `pyproject.toml`. `gh` and `claude` are
      external prerequisites documented in the README.
