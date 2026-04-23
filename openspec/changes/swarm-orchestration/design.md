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
    └─▶ reconcile (read-only; produces a report)
            └─▶ repair_phase (apply report findings)
                    └─▶ fetch_ready
                            ├─[empty]─▶ END (success)
                            └─[ready]─▶ claim_task
                                            ├─[claim fail]─▶ fetch_ready   (next one)
                                            └─[claim ok]──▶ ensure_worktree
                                                                 └─▶ run_worker
                                                                         ├─[success marker]─▶ git_commit_push
                                                                         │                           ├─[clean tree]──▶ fail_task ("worker reported success but left tree clean")
                                                                         │                           │                        └─▶ fetch_ready OR END_fail
                                                                         │                           ├─[push fail]───▶ fail_task (git stderr)
                                                                         │                           │                        └─▶ fetch_ready OR END_fail
                                                                         │                           └─[commit+push ok]──▶ open_pr
                                                                         │                                                         └─▶ close_task
                                                                         │                                                                 └─▶ fetch_ready
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

bd's built-in status vocabulary is `open | in_progress | blocked |
deferred | closed`. Turma does not invent a "failed" status. Retry
state is persisted through bd labels and notes on top of the
standard status flow:

- `turma-retries:<n>` — integer label, incremented on each failed
  attempt. Absent means zero prior attempts.
- `needs_human_review` — label added after retry-budget exhaustion.
  `list_ready_tasks` filters out tasks carrying this label so
  exhausted tasks stop appearing as ready.
- `bd note <id> "<reason>"` — records the failure reason in the
  task's notes; `bd show <id>` surfaces the full history.

On each failure:

1. `bd note <id> "<reason>"` records the error.
2. Read the current `turma-retries:<n>` label (0 if absent).
3. If `n + 1 <= max_retries`: replace with `turma-retries:<n+1>` and
   release the claim (status transitions back to `open`). The outer
   loop continues — the task may be re-attempted if it comes up
   ready again.
4. Else (`n + 1 > max_retries`): remove the `turma-retries:*` label,
   add `needs_human_review`, release the claim. The orchestrator
   halts the whole run so the operator can triage via
   `bd list --label needs_human_review`.

`BeadsAdapter.fail_task(task_id, reason, *, retries_so_far,
max_retries)` encapsulates the note + label dance in a single
method; the orchestrator passes in the budget state it computed
from `max_retries` and the current label value. The adapter chooses
the bd argv for each primitive (note, label add/remove, status
release); the orchestrator does not know those details.

## Beads operations

v1 `BeadsAdapter` additions (pinned by unit tests with subprocess
stubs; exact argv determined by Task 2's `bd --help` verification):

```python
class BeadsAdapter:
    ...
    def list_ready_tasks(self, feature: str) -> tuple[BeadsTaskRef, ...]:
        # OPEN + feature:<name> + no unsatisfied blocker edges +
        # no `needs_human_review` label. Exhausted tasks are
        # filtered out here; operators find them via
        # `bd list --label needs_human_review`.
        ...

    def claim_task(self, task_id: str) -> None:
        # Atomic transition to in_progress. Raises PlanningError
        # with `bd` stderr preserved if the task is not claimable
        # (already claimed, closed, or blocked).
        ...

    def fail_task(
        self,
        task_id: str,
        reason: str,
        *,
        retries_so_far: int,
        max_retries: int,
    ) -> None:
        # Records the reason via `bd note`, updates the
        # turma-retries:<n> label, and either releases the claim
        # back to open (budget remaining) or adds
        # needs_human_review + releases (budget exhausted).
        # Idempotent; safe to retry on adapter-level failure.
        ...

    def retries_so_far(self, task_id: str) -> int:
        # Reads the current turma-retries:<n> label off the task
        # body/labels JSON; returns 0 if absent. The orchestrator
        # calls this before invoking fail_task so it owns the
        # budget accounting.
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

## Git operations contract

Mirrors the other subprocess boundaries (Beads, gh, Claude Code).
`GitAdapter` lives in `src/turma/swarm/git.py` and owns the three git
operations the success path needs between `run_worker` and
`open_pr`.

```python
class GitAdapter:
    def __init__(self, repo_root: Path) -> None: ...
        # shutil.which("git") check + fail-fast if missing.

    def status_is_dirty(self, worktree: Path) -> bool: ...
        # `git -C <worktree> status --porcelain=v1`. True iff the
        # output is non-empty after excluding ignored sentinels.

    def commit_all(self, worktree: Path, message: str) -> str: ...
        # `git -C <worktree> add -A`  →
        # `git -C <worktree> commit -m <message>`  →
        # returns the new commit SHA from rev-parse HEAD.
        # Raises PlanningError with git stderr if the commit fails
        # or there is nothing to commit (refuses empty commits).

    def push_branch(
        self, worktree: Path, branch: str, *, remote: str = "origin"
    ) -> None: ...
        # `git -C <worktree> push --set-upstream <remote> <branch>`.
        # Non-zero exit raises PlanningError with git stderr
        # (auth, non-fast-forward, network).
```

### Commit message template (pinned by tests)

```
[{turma_type}] {task_title}

Closes bd-{task_id}.

Generated by turma run for feature "{feature}".
```

The `{turma_type}` component is the parser-side label carried
through transcription as `turma-type:<t>` (impl / test / docs /
spec). The orchestrator reads it off the Beads task's labels.

### Flow between `run_worker` and `open_pr`

On a successful worker sentinel the orchestrator runs:

1. `status_is_dirty(worktree)`.
   - **False** → the worker claimed success without modifying the
     tree. Treated as a worker failure with reason
     `"worker reported success but left the tree clean"`. Task
     enters the retry path, not the PR path.
   - **True** → proceed to commit.
2. `commit_all(worktree, rendered_template)`. A worker that
   committed its own changes inside the session leaves the tree
   clean; that case is handled by step 1 as a "clean tree" failure
   in v1. Workers that commit on their own (common for Codex-style
   sessions) are v2 concerns — v1 expects Claude Code's default
   "edit files, leave commits to me" behavior.
3. `push_branch(worktree, branch)` — push the task branch to
   `origin`. Push failures (auth, non-fast-forward) abort the
   task via the normal fail_task path; the worktree is left in
   place for triage.

### Credentials

Git auth relies on the operator's existing credentials — ssh key
loaded into the agent, or `gh auth`'s git credential helper.
`GitAdapter` does not manage credentials and does not fall back to
HTTPS-with-token.

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

**Read-only.** The reconciliation module
(`src/turma/swarm/reconciliation.py`) detects ambiguity between Beads
state and the filesystem and returns a typed `ReconciliationReport`.
It never mutates Beads, git, or GitHub state. The orchestrator's
main loop consumes the report and runs an explicit repair phase
before the first `fetch_ready`. Concentrating mutations in the main
loop keeps reconciliation pure (trivially testable with pure
fixtures) and gives the operator a single place to read what the
orchestrator is about to change.

Runs before every invocation (including `--dry-run`; `--dry-run`
prints the report and exits without entering the repair phase or
the main loop). Walks the authority model:

1. Query Beads for tasks labelled `feature:<name>` in state
   `in_progress`. These are the candidate "already claimed" tasks
   from a prior run.
2. For each, classify based on the worktree at
   `./.worktrees/<feature>/<bd-id>/` and any associated branch / PR:

   | Finding key | Trigger | Suggested repair (applied by main loop) |
   | --- | --- | --- |
   | `missing-worktree` | Beads says in_progress, worktree absent | release the claim (`in_progress → open`) so the task can be re-attempted |
   | `completion-pending` | `.task_complete` present, no open PR | run the normal commit/push/open-pr tail and close the Beads task |
   | `completion-pending-with-pr` | `.task_complete` present, PR already open | close the Beads task and remove the worktree |
   | `failure-pending` | `.task_failed` present | pass to `fail_task` with the worker's reason, remove the worktree |
   | `stale-no-sentinels` | worktree + branch exist, no sentinel, no PR | no auto-repair — surface and halt the run, operator decides |
   | `orphan-branch` | branch or remote branch with no corresponding in_progress Beads task | surface only; operator triage |

3. `ReconciliationReport.findings` is an ordered tuple of typed
   dataclasses — one per finding above — so the orchestrator can
   dispatch cleanly without re-parsing strings.

The orchestrator's repair phase:

- Applies every repair marked "applied by main loop" in the table,
  in the order reconciliation surfaced them.
- Halts before `fetch_ready` if any `stale-no-sentinels` finding is
  present (v1 does not guess on ambiguous state).
- Prints every repair action taken, in the same compact per-task
  format used by the normal loop.

Reconciliation stdout from the module itself is a short summary:

```
reconcile: 0 in-progress tasks
reconcile: 1 in-progress task needs repair (bd-abc → completion-pending)
reconcile: 1 in-progress task needs attention (bd-xyz → stale-no-sentinels)
reconcile: 1 orphan branch found (task/foo/bd-old)
```

Repair mutations are printed by the main loop when it applies them,
not by reconciliation itself.

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
