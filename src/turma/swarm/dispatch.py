"""Concurrent multi-pool dispatcher for ``turma run`` (parallel execution).

Runs a feature's ready Beads tasks concurrently across provider pools:

- up to ``max_parallel`` top-level worker slots run at once, each also gated by
  its pool's per-pool cap (``Semaphore``);
- a task's ``turma-type:`` selects its pool (``router``); the pool's backend
  resolves a worker via ``services.worker_for``;
- **one global mutation lock** serializes every shared-state access — all
  ``BeadsAdapter`` calls (one Dolt DB) and all shared-``.git`` metadata ops
  (worktree add/remove). The worker subprocess and the network push/PR run
  *outside* the lock, so worker execution is genuinely concurrent;
- on a halting failure (retry budget exhausted), scheduling stops but in-flight
  workers are **drained** to their normal terminal (commit/push/PR or
  ``fail_task``) — never cancelled — then the run raises.

Workers are synchronous (`WorkerBackend.run` blocks), so concurrency uses
threads; the lock is a `threading.Lock`. This is the dispatcher core; wiring it
into ``run_swarm`` / the CLI is a separate step.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from turma.errors import PlanningError
from turma.swarm.pools import PoolRouter
from turma.swarm.worker import WorkerInvocation

if TYPE_CHECKING:  # avoid an import cycle with _orchestrator
    from turma.swarm._orchestrator import SwarmServices


def dispatch_concurrent(
    feature: str,
    services: "SwarmServices",
    *,
    router: PoolRouter,
    max_parallel: int,
) -> None:
    """Run ``feature``'s ready tasks concurrently across provider pools.

    Raises ``PlanningError`` if a task exhausts its retry budget (after draining
    in-flight workers) — matching the sequential loop's halt.
    """
    # Imported lazily: keeps module import cheap and avoids any cycle if
    # _orchestrator ever imports this module. These are the same pipeline
    # primitives the sequential `_run_single_task` uses.
    from turma.swarm._orchestrator import (
        CLEAN_TREE_REASON,
        _clear_sentinels,
        _handle_failure,
        _pr_number_from_url,
        _render_commit_message,
        _render_pr_body,
        _render_pr_title,
        _revert_beads_export,
        _turma_type_of,
    )

    if services.worker_for is None:
        raise PlanningError(
            "dispatch_concurrent requires services.worker_for (a "
            "backend -> WorkerBackend resolver)"
        )

    lock = threading.Lock()
    global_slots = threading.Semaphore(max_parallel)
    pool_slots = {pool.name: threading.Semaphore(pool.max) for pool in router.pools}
    halt = threading.Event()
    halt_error: list[PlanningError] = []
    threads: list[threading.Thread] = []

    def signal_halt(task_id: str) -> None:
        if not halt.is_set():
            halt_error.append(
                PlanningError(
                    f"retry budget exhausted on {task_id}; halting run. "
                    f"Triage with `bd show {task_id}` and "
                    "`bd list --label needs_human_review`."
                )
            )
            halt.set()

    def fail(task_id: str, reason: str) -> None:
        with lock:
            exhausted = _handle_failure(services, task_id, reason)
        if exhausted:
            signal_halt(task_id)

    def run_task(task, pool) -> None:
        try:
            # Worktree add is a shared-.git metadata op → under the lock.
            with lock:
                ref = services.worktree.setup(
                    feature=feature,
                    task_id=task.id,
                    base_branch=services.base_branch,
                )
            services.emitter.emit("worktree_setup", task_id=task.id)
            _clear_sentinels(ref.path)
            with lock:
                description = services.beads.get_task_body(task.id)

            invocation = WorkerInvocation(
                task_id=task.id,
                title=task.title,
                description=description,
                worktree=ref.path,
                timeout_seconds=services.worker_timeout,
            )
            worker = services.worker_for(pool.backend)
            services.emitter.emit(
                "worker_running",
                task_id=task.id,
                timeout_s=services.worker_timeout,
            )
            result = worker.run(invocation)  # outside the lock — concurrent

            if result.status != "success":
                fail(task.id, result.reason or f"worker {result.status}")
                return
            if not services.git.status_is_dirty(ref.path):
                fail(task.id, CLEAN_TREE_REASON)
                return

            try:
                with lock:
                    services.git.commit_all_with_bd_export(
                        ref.path,
                        _render_commit_message(task, feature),
                        beads=services.beads,
                        repo_root=services.repo_root,
                    )
                services.emitter.emit("commit", task_id=task.id)
                services.git.push_branch(ref.path, ref.branch)  # network
                services.emitter.emit("push", task_id=task.id)
                pr_url = services.pr.open_pr(  # network
                    branch=ref.branch,
                    base=services.base_branch,
                    title=_render_pr_title(task),
                    body=_render_pr_body(task, description),
                )
            except PlanningError as exc:
                fail(task.id, str(exc))
                return

            pr_number = _pr_number_from_url(pr_url)
            with lock:
                services.beads.mark_pr_open(task.id, pr_number)
                _revert_beads_export(services)
            services.emitter.emit(
                "task_opened",
                task_id=task.id,
                pr_number=pr_number,
                pr_url=pr_url,
            )
        finally:
            pool_slots[pool.name].release()
            global_slots.release()

    # Scheduling loop: claim ready tasks and hand each to a worker thread until
    # no ready work remains or a halt is signalled. Only this thread claims, so
    # there is no double-claim; a lost claim race just re-fetches.
    while not halt.is_set():
        global_slots.acquire()
        if halt.is_set():
            global_slots.release()
            break
        with lock:
            ready = services.beads.list_ready_tasks(feature)
            task = ready[0] if ready else None
        if task is None:
            global_slots.release()
            break  # no ready tasks remain → drain in-flight and finish

        pool = router.pool_for(_turma_type_of(task))
        pool_slots[pool.name].acquire()
        with lock:
            try:
                services.beads.claim_task(task.id)
            except PlanningError as exc:
                services.emitter.emit(
                    "claim_race", task_id=task.id, detail=str(exc)
                )
                pool_slots[pool.name].release()
                global_slots.release()
                continue
            _revert_beads_export(services)
        services.emitter.emit("task_claimed", task_id=task.id, title=task.title)

        thread = threading.Thread(
            target=run_task, args=(task, pool), daemon=True
        )
        thread.start()
        threads.append(thread)

    # Drain: let every in-flight worker reach its normal terminal, then halt.
    for thread in threads:
        thread.join()
    if halt_error:
        raise halt_error[0]
