"""Swarm orchestrator for `turma run` — single-feature sequential loop.

Drives one Beads task at a time from `ready` to `closed` (or `failed`
with a retry-budget decision). Each iteration follows the committed
state-machine contract in
`openspec/changes/swarm-orchestration/design.md`:

    preflight → reconcile (read-only) → repair_phase → main_loop

The module is strictly adapter-driven — every external effect
(bd / git / gh / worker CLI) goes through `SwarmServices`. Tests
inject stubs directly and assert on the call sequence; there is no
live subprocess in this module's unit-test scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from turma.errors import PlanningError
from turma.swarm.git import COMMIT_MESSAGE_TEMPLATE, GitAdapter
from turma.swarm.pull_request import PullRequestAdapter
from turma.swarm.worker import get_worker_backend
from turma.swarm.worktree import WorktreeManager, WorktreeRef
from turma.swarm.reconciliation import (
    CompletionPending,
    CompletionPendingWithPr,
    FailurePending,
    MissingWorktree,
    OrphanBranch,
    ReconciliationReport,
    StaleNoSentinels,
    reconcile_feature,
)
from turma.swarm.worker import WorkerBackend, WorkerInvocation
from turma.transcription.beads import BeadsAdapter, BeadsTaskRef


CLEAN_TREE_REASON = "worker reported success but left the tree clean"
TURMA_TYPE_LABEL_PREFIX = "turma-type:"
_DEFAULT_TURMA_TYPE = "impl"


# ---------------------------------------------------------------------
# SwarmServices — DI container
# ---------------------------------------------------------------------


DEFAULT_WORKER_BACKEND = "claude-code"


@dataclass
class SwarmServices:
    """Dependency-injection boundary for the swarm orchestrator.

    Mirrors the `PlanningServices` / transcription shapes. Tests pass
    stubs directly; the CLI (Task 8) constructs the real adapters.
    """

    beads: BeadsAdapter
    worktree: WorktreeManager
    git: GitAdapter
    pr: PullRequestAdapter
    worker_factory: Callable[[], WorkerBackend]
    repo_root: Path
    base_branch: str = "main"
    max_retries: int = 1
    worker_timeout: int = 1800


def default_swarm_services(
    repo_root: Path,
    *,
    backend: str = DEFAULT_WORKER_BACKEND,
    base_branch: str = "main",
    max_retries: int = 1,
    worker_timeout: int = 1800,
    worktree_root: str = ".worktrees",
) -> SwarmServices:
    """Construct production `SwarmServices` rooted at `repo_root`.

    Each adapter preflights its CLI dependency at construction — `bd`
    for Beads, `git` for worktree + git operations, `gh` (plus an
    authenticated session via `gh auth status`) for the PR adapter.
    A missing or misconfigured dependency surfaces as a
    `PlanningError` here so the CLI can exit 1 before any Beads
    state is touched.

    The worker backend is resolved lazily via
    `get_worker_backend(backend)`: the `claude` CLI check only runs
    when the orchestrator actually claims a task and instantiates a
    worker, so `--dry-run` does not require Claude Code to be
    installed.
    """
    return SwarmServices(
        beads=BeadsAdapter(),
        worktree=WorktreeManager(
            repo_root=repo_root, worktree_root=worktree_root
        ),
        git=GitAdapter(),
        pr=PullRequestAdapter(),
        worker_factory=lambda: get_worker_backend(backend),
        repo_root=repo_root,
        base_branch=base_branch,
        max_retries=max_retries,
        worker_timeout=worker_timeout,
    )


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------


def run_swarm(
    feature: str,
    *,
    services: SwarmServices | None = None,
    max_tasks: int | None = None,
    backend: str | None = None,
    dry_run: bool = False,
) -> None:
    """Run the single-feature sequential swarm loop for `feature`.

    - `services` is required for orchestration. Task 8 wires default
      construction behind the CLI; callers that invoke `run_swarm`
      programmatically must provide a `SwarmServices`.
    - `max_tasks` caps outer-loop iterations (default unbounded).
    - `backend` is accepted for the signature Task 8 wants; backend
      selection actually happens via the provided
      `services.worker_factory` and this parameter is purely
      informational in v1 (raises if it names something the factory
      is not known to produce). v1 ships only `claude-code`.
    - `dry_run=True` runs preflight + reconciliation only; the
      reconciliation summary has already been printed by
      `reconcile_feature` at that point, so dry-run exits cleanly
      without entering the repair phase or the main loop.
    """
    if services is None:
        raise PlanningError(
            "run_swarm requires a SwarmServices instance. The CLI "
            "wires default services in Task 8 of "
            "openspec/changes/swarm-orchestration/tasks.md."
        )
    if backend is not None and backend != "claude-code":
        raise PlanningError(
            f"unknown worker backend: {backend!r}. v1 registers only "
            "'claude-code'."
        )

    _preflight(feature, services.repo_root)

    report = reconcile_feature(
        feature,
        adapter=services.beads,
        worktree_manager=services.worktree,
        git_adapter=services.git,
        pr_adapter=services.pr,
        repo_root=services.repo_root,
    )

    if dry_run:
        return

    _apply_repairs(feature, report, services)
    _main_loop(feature, services, max_tasks)


# ---------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------


def _preflight(feature: str, repo_root: Path) -> None:
    """Verify spec + approval + transcription artifacts exist.

    Pointed error messages tell the operator which prior `turma`
    command is missing so they can resume at the right point.
    """
    change_dir = repo_root / "openspec" / "changes" / feature
    if not change_dir.is_dir():
        raise PlanningError(
            f"no OpenSpec change directory for feature {feature!r} at "
            f"{change_dir}. Run `turma plan --feature {feature}` first."
        )
    if not (change_dir / "APPROVED").exists():
        raise PlanningError(
            f"feature {feature!r} is not APPROVED. Run "
            f"`turma plan --feature {feature}` and complete the "
            "author/critic loop."
        )
    if not (change_dir / "TRANSCRIBED.md").exists():
        raise PlanningError(
            f"feature {feature!r} has not been transcribed to Beads. "
            f"Run `turma plan-to-beads --feature {feature}` first."
        )


# ---------------------------------------------------------------------
# Repair phase
# ---------------------------------------------------------------------


def _apply_repairs(
    feature: str,
    report: ReconciliationReport,
    services: SwarmServices,
) -> None:
    """Apply the repair documented for each finding, in order.

    Halts before the main loop when:

    - any `stale-no-sentinels` finding is present (v1 never guesses
      on ambiguous state), or
    - any finding that calls `fail_task` exhausts the retry budget
      — repair-phase exhaustions must halt just like main-loop
      exhaustions (tasks.md Task 7 budget rule).

    Exhausted ids are collected across the whole repair phase so the
    operator sees every repair the orchestrator attempted before the
    halt fires, rather than halting on the first one and hiding the
    rest.
    """
    exhausted_ids: list[str] = []

    for finding in report.findings:
        match finding:
            case MissingWorktree(task_id=task_id):
                if _handle_failure(
                    services,
                    task_id,
                    "reconcile: worktree missing; releasing claim",
                ):
                    exhausted_ids.append(task_id)
                print(f"repair: {task_id} → release claim (missing-worktree)")

            case CompletionPending(task_id=task_id):
                pr_url = _complete_pending_task(feature, task_id, services)
                print(
                    f"repair: {task_id} → committed, pushed, PR opened "
                    f"({pr_url}), closed"
                )

            case CompletionPendingWithPr(task_id=task_id, pr_url=pr_url):
                ref = _ref_for(feature, task_id, services)
                services.beads.close_task(task_id)
                services.worktree.cleanup(ref)
                print(
                    f"repair: {task_id} → closed (PR already open at "
                    f"{pr_url}), worktree removed"
                )

            case FailurePending(task_id=task_id, reason=reason):
                if _handle_failure(
                    services, task_id, f"reconcile: {reason}"
                ):
                    exhausted_ids.append(task_id)
                print(f"repair: {task_id} → fail_task recorded ({reason})")

            case StaleNoSentinels(task_id=task_id):
                raise PlanningError(
                    f"stale worktree for {task_id} has no sentinels; "
                    f"operator decides. Inspect "
                    f"`bd show {task_id}` and "
                    f"`.worktrees/{feature}/{task_id}/` before "
                    "re-running."
                )

            case OrphanBranch(branch=branch):
                print(f"repair: orphan branch (operator triage): {branch}")

    if exhausted_ids:
        joined = ", ".join(exhausted_ids)
        raise PlanningError(
            f"retry budget exhausted on {joined} during repair phase; "
            "halting run. Triage with `bd list --label "
            "needs_human_review`."
        )


# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------


def _main_loop(
    feature: str,
    services: SwarmServices,
    max_tasks: int | None,
) -> None:
    """fetch_ready → claim → worktree → worker → commit/push/PR/close.

    Exits cleanly when no ready tasks remain. Halts with a
    PlanningError if a task exhausts its retry budget, matching the
    `END_fail` terminal in the state machine diagram.
    """
    iterations = 0
    while True:
        if max_tasks is not None and iterations >= max_tasks:
            print(f"swarm: stopping at --max-tasks={max_tasks}")
            return

        ready = services.beads.list_ready_tasks(feature)
        if not ready:
            print("swarm: no ready tasks remain; done")
            return

        task = ready[0]

        try:
            services.beads.claim_task(task.id)
        except PlanningError as exc:
            # Claim race — another actor beat us. Skip this task and
            # re-fetch on the next iteration. Races do NOT consume
            # `max_tasks` budget: the operator asked for N tasks
            # end-to-end, not N claim attempts.
            print(f"swarm: claim race on {task.id}; skipping ({exc})")
            continue

        iterations += 1
        print(f"swarm: claimed {task.id} — {task.title}")

        exhausted = _run_single_task(feature, task, services)
        if exhausted:
            raise PlanningError(
                f"retry budget exhausted on {task.id}; halting run. "
                f"Triage with `bd show {task.id}` and "
                "`bd list --label needs_human_review`."
            )


def _run_single_task(
    feature: str,
    task: BeadsTaskRef,
    services: SwarmServices,
) -> bool:
    """Drive one claimed task through the state machine.

    Returns True iff the run halted on exhausted retry budget and the
    outer loop must stop.
    """
    ref = services.worktree.setup(
        feature=feature,
        task_id=task.id,
        base_branch=services.base_branch,
    )
    description = services.beads.get_task_body(task.id)
    invocation = WorkerInvocation(
        task_id=task.id,
        title=task.title,
        description=description,
        worktree=ref.path,
        timeout_seconds=services.worker_timeout,
    )
    worker = services.worker_factory()
    result = worker.run(invocation)

    if result.status != "success":
        reason = result.reason or f"worker {result.status}"
        return _handle_failure(services, task.id, reason)

    if not services.git.status_is_dirty(ref.path):
        return _handle_failure(services, task.id, CLEAN_TREE_REASON)

    try:
        message = _render_commit_message(task, feature)
        services.git.commit_all(ref.path, message)
        services.git.push_branch(ref.path, ref.branch)
    except PlanningError as exc:
        return _handle_failure(services, task.id, str(exc))

    try:
        pr_url = services.pr.open_pr(
            branch=ref.branch,
            base=services.base_branch,
            title=_render_pr_title(task),
            body=_render_pr_body(task, description),
        )
    except PlanningError as exc:
        return _handle_failure(services, task.id, str(exc))

    services.beads.close_task(task.id)
    services.worktree.cleanup(ref)
    print(f"swarm: closed {task.id} (PR: {pr_url})")
    return False


# ---------------------------------------------------------------------
# Failure + budget helpers
# ---------------------------------------------------------------------


def _handle_failure(
    services: SwarmServices,
    task_id: str,
    reason: str,
) -> bool:
    """Record a task failure and return True iff the budget is exhausted."""
    retries = services.beads.retries_so_far(task_id)
    services.beads.fail_task(
        task_id,
        reason,
        retries_so_far=retries,
        max_retries=services.max_retries,
    )
    exhausted = (retries + 1) > services.max_retries
    if exhausted:
        print(
            f"swarm: {task_id} failed (budget exhausted after "
            f"{retries + 1} attempts): {reason}"
        )
    else:
        print(
            f"swarm: {task_id} failed (attempt "
            f"{retries + 1}/{services.max_retries + 1}): {reason}"
        )
    return exhausted


# ---------------------------------------------------------------------
# Reconcile-repair helpers
# ---------------------------------------------------------------------


def _complete_pending_task(
    feature: str,
    task_id: str,
    services: SwarmServices,
) -> str:
    """Run the normal commit/push/open-pr tail for a
    reconciliation-detected completion-pending task, then close it."""
    ref = _ref_for(feature, task_id, services)
    task = _lookup_task(services.beads, feature, task_id)
    description = services.beads.get_task_body(task_id)
    message = _render_commit_message(task, feature)
    services.git.commit_all(ref.path, message)
    services.git.push_branch(ref.path, ref.branch)
    pr_url = services.pr.open_pr(
        branch=ref.branch,
        base=services.base_branch,
        title=_render_pr_title(task),
        body=_render_pr_body(task, description),
    )
    services.beads.close_task(task_id)
    services.worktree.cleanup(ref)
    return pr_url


def _ref_for(
    feature: str, task_id: str, services: SwarmServices
) -> WorktreeRef:
    return WorktreeRef(
        path=services.worktree.worktree_path_for(feature, task_id),
        branch=services.worktree.branch_name_for(feature, task_id),
    )


def _lookup_task(
    beads: BeadsAdapter, feature: str, task_id: str
) -> BeadsTaskRef:
    """Re-hydrate a BeadsTaskRef for a repair-phase task.

    Reconciliation observed the task by id; we need its title/labels
    for the commit/PR templates. Falls back to a minimal ref if the
    task no longer appears in feature listings (shouldn't happen, but
    avoids a crash during repair).
    """
    for ref in beads.list_in_progress_tasks(feature):
        if ref.id == task_id:
            return ref
    return BeadsTaskRef(id=task_id, title=task_id, labels=())


# ---------------------------------------------------------------------
# Commit / PR templates
# ---------------------------------------------------------------------


def _turma_type_of(task: BeadsTaskRef) -> str:
    for label in task.labels:
        if label.startswith(TURMA_TYPE_LABEL_PREFIX):
            return label[len(TURMA_TYPE_LABEL_PREFIX):] or _DEFAULT_TURMA_TYPE
    return _DEFAULT_TURMA_TYPE


def _render_commit_message(task: BeadsTaskRef, feature: str) -> str:
    return COMMIT_MESSAGE_TEMPLATE.format(
        turma_type=_turma_type_of(task),
        task_title=task.title,
        task_id=task.id,
        feature=feature,
    )


def _render_pr_title(task: BeadsTaskRef) -> str:
    return f"[{_turma_type_of(task)}] {task.title}"


def _render_pr_body(task: BeadsTaskRef, description: str) -> str:
    tail = description.strip()
    if tail:
        return f"Closes {task.id}.\n\n{tail}"
    return f"Closes {task.id}."
