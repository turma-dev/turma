## Scope

Covers a single-feature sequential execution loop on top of the Beads
DAG produced by `turma plan-to-beads`. Starts when the operator runs
`turma run --feature <name>`; ends when:

- no open ready tasks remain for the feature (all done or blocked), or
- a task fails and the configured retry budget is exhausted, or
- the operator interrupts the loop (Ctrl-C / process kill).

The loop opens one PR per Beads task against a configured base branch
and stops. Review, merge, and release are human-driven.

Out of scope for v1 (all deferred to follow-on capabilities): parallel
execution, multi-backend worker routing, provider-pool scheduling,
retry policies beyond `max_retries`, cross-feature concurrency, review
or merge automation.

## Authority model

1. **Beads** is the task DAG and the authoritative record of which
   tasks are open / claimed / closed / failed.
2. **Git worktree + branch** is the authoritative record of work in
   flight for a claimed task.
3. **GitHub PR state** is the authoritative record of a submitted
   task's integration status.
4. **Turma run sentinel files** (`.task_complete`, `.task_progress`,
   `.task_failed` inside the worktree) are worker-to-orchestrator
   signals only — not authoritative current state.

Reconciliation at run-start walks these sources in order and resolves
ambiguity explicitly (see `## Reconciliation` below).

## Command surface

```
turma run --feature <name>
turma run --feature <name> --max-tasks N       # bound the inner loop
turma run --feature <name> --backend <id>      # override [swarm].worker_backend
turma run --feature <name> --dry-run           # print the plan; no claim, no work
```

Behavior:

- **Preflight**: ensure `openspec/changes/<name>/` exists, `APPROVED`
  is present, `TRANSCRIBED.md` is present. Absence of any of these
  halts with a specific error pointing at `turma plan` or
  `turma plan-to-beads` as the missing prior step.
- **Reconciliation** (always runs, even on a clean tree): see
  `## Reconciliation`.
- **Main loop** (unless `--dry-run`): while ready tasks remain and the
  budget holds, claim → work → commit → PR → mark done → next. Each
  task is sequential; no parallelism.
- `--max-tasks` caps the loop iterations so an operator can smoke one
  task end-to-end before opening the floodgates. Default is unbounded
  (runs until no ready work).
- `--backend` overrides the configured worker backend for this
  invocation. The backend must be registered; unknown ids raise
  before any task is claimed.
- `--dry-run` prints the ready-task list, the would-be worktree
  paths, the would-be branch names, and exits. No `bd` state change,
  no git worktree add, no `gh pr create`.

## State machine

```
preflight_check
    └─▶ reconcile
            └─▶ fetch_ready
                    ├─[empty]─▶ END (success)
                    └─[ready]─▶ claim_task
                                    ├─[claim fail]─▶ fetch_ready   (next one)
                                    └─[claim ok]──▶ ensure_worktree
                                                         └─▶ run_worker
                                                                 ├─[success marker]─▶ commit_branch
                                                                 │                           └─▶ open_pr
                                                                 │                                   └─▶ close_task
                                                                 │                                           └─▶ fetch_ready
                                                                 ├─[fail marker]──────▶ fail_task
                                                                 │                           └─▶ fetch_ready OR END_fail
                                                                 └─[timeout]─────────▶ fail_task
                                                                                             └─▶ fetch_ready OR END_fail
```

Terminal conditions for the outer loop:
- `END (success)` — no ready tasks remain.
- `END_fail` — a task failed and the budget for its retries is
  exhausted; orchestrator halts the whole run so the operator can
  triage.

### Retry budget

Each task has at most `max_retries + 1` attempts. On the first
failure, Beads records the failure, the worktree is torn down, and
the outer loop continues so the task can be re-attempted if it comes
up ready again (it will, since a failed task is reopened). On
exhaustion, the task is left in a "failed" Beads state with
`needs_human_review` label and the outer loop halts.

## Beads operations

v1 `BeadsAdapter` additions (pinned by unit tests with subprocess
stubs; exact argv determined by Task 2's `bd --help` verification):

```python
class BeadsAdapter:
    ...
    def list_ready_tasks(self, feature: str) -> tuple[BeadsTaskRef, ...]:
        # OPEN + feature:<name> + no unsatisfied blocker edges.
        ...

    def claim_task(self, task_id: str) -> None:
        # Atomic transition to "in progress". Raises PlanningError
        # with `bd` stderr preserved if the task is not claimable
        # (already claimed, closed, or blocked).
        ...

    def fail_task(self, task_id: str, reason: str) -> None:
        # Records a failure event and reopens the task so a later
        # run can retry within the budget. Attaches the reason as a
        # note / event payload so `bd show` surfaces it.
        ...
```

`close_task(task_id)` already exists from `beads-transcription` and is
reused on success.

The exact `bd` subcommands backing `list_ready_tasks` and `claim_task`
are Task 2's verification deliverable — candidates include bd's
`ready` view (if present in 1.0.2+), `bd list` with blocker-filter
flags, `bd start`, `bd set-state`, `bd assign`, and similar. Task 2
documents the chosen commands in the adapter's class docstring with
the same argv-pinning test pattern used for `create_task`.

## Worktree contract

Layout:

```
<repo-root>/
  .worktrees/
    <feature>/
      <bd-id>/             ← separate working tree on branch task/<feature>/<bd-id>
        .task_complete     ← worker sentinel (optional)
        .task_progress     ← worker heartbeat (optional; last-modified drives stuck detection)
        .task_failed       ← worker sentinel (optional; holds a reason)
```

Operations:

- `WorktreeManager.setup(feature, task_id, base_branch)` → `Path`:
  - If the worktree path already exists and is registered, return it
    (reconciliation path).
  - Else `git worktree add <path> -b task/<feature>/<task-id>
    <base_branch>` and return the new path.
  - Branch name collisions (existing stale branch) raise
    `PlanningError` with the exact `git worktree add` stderr.
- `WorktreeManager.cleanup(path)` removes the worktree and its
  branch. Called only on task success. Failed worktrees are left in
  place for triage; reconciliation picks them up on the next run.

`.worktrees/` is gitignored in the orchestrator's repo (it's
working-copy state, not history).

## Worker backend protocol

```python
@dataclass(frozen=True)
class WorkerInvocation:
    task_id: str
    title: str
    description: str          # verbatim subtask list from the Beads task body
    worktree: Path
    timeout_seconds: int


@dataclass(frozen=True)
class WorkerResult:
    status: Literal["success", "failure", "timeout"]
    reason: str               # empty on success
    stdout: str
    stderr: str


class WorkerBackend(Protocol):
    name: str

    def run(self, invocation: WorkerInvocation) -> WorkerResult: ...
```

### Claude Code implementation (v1 pinned)

- argv: `claude -p <prompt> --cwd <worktree>
  --dangerously-skip-permissions`. The `--dangerously-skip-permissions`
  flag is how Claude Code accepts automated operation inside a
  sandboxed worktree; the operator opts in by installing Claude Code
  and configuring the worker backend.
- Prompt template (module-level constant, rendered with the Beads
  task fields):
  ```
  You are a Turma worker agent. Work inside {worktree}.

  Task: {title}

  Acceptance criteria:
  {description}

  When you believe the task is complete, write `DONE` to
  `.task_complete` in this directory. If you hit a blocker you can
  not resolve, write the reason to `.task_failed` and stop.
  ```
- Result detection: after Claude Code exits, inspect the worktree
  for the sentinels:
  - `.task_complete` exists → `status = "success"`.
  - `.task_failed` exists → `status = "failure"`, reason = file
    contents.
  - Neither exists → `status = "failure"`, reason = "worker exited
    without writing a completion marker".
- Timeout: `timeout_seconds` is enforced by `subprocess.run`. Exceeding
  it → `status = "timeout"`.

Other backends (Codex, OpenCode, Gemini) are explicitly v2 concerns.
The protocol is deliberately minimal so adding them is a small branch.

## Pull request adapter

```python
class PullRequestAdapter:
    def __init__(self) -> None: ...
        # validates shutil.which("gh") and shells `gh auth status`
        # once to confirm an authenticated session.

    def open_pr(
        self,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        # returns the PR URL on success.
        ...
```

Invocation: `gh pr create --head <branch> --base <base> --title
<title> --body <body>`. Non-zero exit raises `PlanningError` with
`gh` stderr preserved. The adapter does NOT retry on transient GitHub
failures in v1.

The PR title / body are derived from the Beads task:

- Title: `[{turma-type}] {task title}`. Example:
  `[impl] Extract planner primitives`.
- Body: `Closes bd-<id>.\n\n{task description verbatim}`.

(The `Closes bd-<id>` line is a convention for later automation;
nothing in the orchestrator consumes it yet.)

## Reconciliation

Runs before every invocation (including `--dry-run`). Walks the
authority model bottom-up:

1. Query Beads for tasks labelled `feature:<name>` and `in progress`.
   These are the candidate "already claimed" tasks.
2. For each, look at `./.worktrees/<feature>/<bd-id>/`:
   - Missing → the worktree was deleted or never created.
     Re-open the Beads task (fail back to "open") and surface the
     discrepancy in stdout.
   - Present + no sentinels → stale; check if a branch still exists
     locally or remotely. If yes and a PR exists, advance Beads to
     closed on the operator's next command (but NOT automatically —
     v1 surfaces and halts rather than guessing).
   - Present + `.task_complete` → finish the task: commit/push if
     needed, open PR if none exists, close Beads task.
   - Present + `.task_failed` → record the failure against Beads
     (within retry budget) and clean up the worktree.
3. Stale branches without corresponding Beads tasks are listed but
   NOT auto-deleted; operator triage required.

Reconciliation output is a short summary printed to stdout:

```
reconcile: 0 in-progress tasks
reconcile: 1 in-progress task resolved (bd-abc → closed via PR #42)
reconcile: 1 in-progress task needs attention (bd-xyz → worktree missing)
```

## Filesystem markers

- `.worktrees/<feature>/<bd-id>/.task_complete` — worker wrote this;
  orchestrator closes the Beads task and opens a PR.
- `.worktrees/<feature>/<bd-id>/.task_failed` — worker wrote this;
  contents are the failure reason. Orchestrator fails the Beads task
  within budget, removes the worktree, continues.
- `.worktrees/<feature>/<bd-id>/.task_progress` — worker heartbeat
  for stuck detection (optional, last-modified driven). If the
  worker writes nothing here for 2×`worker_timeout`, the task times
  out.

All three are gitignored. None is authoritative for recovery —
Beads state + git branch + PR state dominate.

## Config surface

New `[swarm]` block in `turma.example.toml` (all keys optional, with
defaults):

```toml
[swarm]
# Which worker backend drives claimed tasks. v1: "claude-code" only.
worker_backend = "claude-code"

# Seconds before a worker is considered timed-out.
worker_timeout = 1800

# Per-task retries before the orchestrator halts. 0 disables retry.
max_retries = 1

# Where to put per-task worktrees. Paths are relative to the repo
# root unless absolute.
worktree_root = ".worktrees"

# Base branch for PRs opened by the orchestrator.
base_branch = "main"
```

Config loader enforces:
- `worker_backend` must be registered (v1: only `"claude-code"`).
- `max_retries >= 0`.
- `worker_timeout > 0`.
- `worktree_root` is a non-empty string.

## Error surface

All failures raise `PlanningError` consistent with the rest of the
Turma CLI. Categories:

- Preflight: missing `APPROVED`, missing `TRANSCRIBED.md`, missing
  `openspec/changes/<feature>/`.
- Prerequisite: `bd` / `gh` / worker backend CLI missing, with the
  install hint for each.
- Beads: claim failed (task no longer ready), adapter non-zero exit
  with `bd` stderr.
- Worker: `subprocess` failure, timeout, missing completion marker.
- Git: worktree add failed, branch collision, commit/push failed.
- GitHub: `gh pr create` non-zero exit with `gh` stderr.
- Reconciliation: unresolved in-progress task ("needs attention").

Every error message names the feature, the task id (when known), and
the recovery path (`turma run --feature X` to resume, or a manual
`bd`/`git`/`gh` command to fix the specific ambiguity).

## Open items deferred past v1

- Parallel task execution (concurrent `bd claim` + worker processes).
- Per-task backend routing via `bd` labels
  (`worker-backend:claude-code`, etc.).
- Codex / OpenCode / Gemini worker implementations.
- Provider-pool scheduling, rate-limit awareness, cost tracking.
- Automated rollback or half-commit recovery when PR creation fails
  mid-flight.
- Post-merge behavior (advancing the Beads DAG after review, release
  automation).
- A `turma status` dashboard that inspects live orchestrator state
  across features.
