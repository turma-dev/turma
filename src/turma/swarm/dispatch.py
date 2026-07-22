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
- the scheduler picks a *currently-schedulable* ready task (one whose pool has
  a free slot) rather than blocking on ``ready[0]``'s pool, so a saturated pool
  never stalls ready work in a pool that still has capacity;
- every halt (retry budget exhausted, or an unexpected worker-thread exception)
  is set under the mutation lock, and the scheduler checks halt under that same
  lock immediately before it claims — so a task is claimed only when it will be
  started (never claimed-then-abandoned), and no new task is claimed once a halt
  is recorded;
- on a halt, scheduling stops but in-flight workers are **drained** to their
  normal terminal (commit/push/PR or ``fail_task``) — never cancelled — then
  the run raises.

Workers are synchronous (`WorkerBackend.run` blocks), so concurrency uses
threads. Slot accounting lives under a `threading.Condition` (`cv`): counters
guarded by it, and workers notify it on release so the scheduler wakes exactly
when a slot frees. The mutation lock is a separate `threading.Lock`; the two are
never held nested. This is the dispatcher core; wiring it into ``run_swarm`` /
the CLI is a separate step.
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

    lock = threading.Lock()  # serializes all shared bd + .git mutation
    cv = threading.Condition()  # guards the slot counters below
    global_running = 0
    pool_running = {pool.name: 0 for pool in router.pools}
    halt = threading.Event()
    halt_error: list[PlanningError] = []
    # An unexpected exception in a worker thread (anything outside the normal
    # worker-failure / commit-failure paths) must not vanish with the thread and
    # silently drop a claimed task. Record the first, halt, and re-raise it after
    # draining — a code/infra bug surfaces rather than a task going missing.
    fatal_error: list[BaseException] = []
    threads: list[threading.Thread] = []

    def release_slot(pool_name: str) -> None:
        """Give back one global + one pool slot and wake the scheduler."""
        nonlocal global_running
        with cv:
            global_running -= 1
            pool_running[pool_name] -= 1
            cv.notify_all()

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
        # signal_halt runs INSIDE the lock, atomically with recording the
        # failure. The scheduler checks halt under this same lock before it
        # claims (below), so once exhaustion is recorded no further task is
        # claimed or started — closing the retry-exhaustion race where the
        # scheduler, holding the lock to claim, would read halt as still-false
        # and start an extra task before the failing worker could set it.
        with lock:
            if _handle_failure(services, task_id, reason):
                signal_halt(task_id)

    def run_task(task, pool) -> None:
        nonlocal global_running
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
            if not services.git.status_is_dirty(ref.path, ignore_bd_export=True):
                fail(task.id, CLEAN_TREE_REASON)
                return

            try:
                with lock:
                    services.git.commit_worker_changes(
                        ref.path, _render_commit_message(task, feature)
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
        except Exception as exc:  # noqa: BLE001 — surface, never swallow
            # An error the pipeline does not model as a task failure (worktree
            # setup, body fetch, worker resolution, PR-number parse, an
            # unexpected bd/git fault). Halt and carry it to the drain so it is
            # raised, not lost with this thread. Set halt under the lock so the
            # scheduler's under-lock claim check sees it atomically, same as the
            # retry-exhaustion path. (This except runs with no lock held: every
            # `with lock` block above has exited by the time it fires.)
            with lock:
                if not fatal_error:
                    fatal_error.append(exc)
                halt.set()
        finally:
            release_slot(pool.name)

    # Scheduling loop: repeatedly pick a ready task that a free slot can run
    # right now — global slots below max_parallel AND its pool below its cap —
    # and hand it to a worker thread. Only this thread claims, so there is no
    # double-claim; a lost claim race just re-fetches. Picking a *schedulable*
    # task (not strictly ready[0]) keeps a saturated pool from stalling ready
    # work in a pool with capacity.
    while not halt.is_set():
        with lock:
            ready = services.beads.list_ready_tasks(feature)
        if halt.is_set():
            break

        with cv:
            task = None
            pool = None
            for candidate in ready:
                candidate_pool = router.pool_for(_turma_type_of(candidate))
                if (
                    global_running < max_parallel
                    and pool_running[candidate_pool.name] < candidate_pool.max
                ):
                    task, pool = candidate, candidate_pool
                    global_running += 1
                    pool_running[pool.name] += 1
                    break
            if task is None:
                # Nothing schedulable right now. If nothing is in flight either,
                # no in-flight worker can free a slot or re-ready a retried task,
                # so we are done — drain (a no-op) and finish. Otherwise wait for
                # a worker to release a slot (a retry may re-ready a task too);
                # the timeout is a backstop, releases notify us directly.
                if global_running == 0:
                    break
                cv.wait(timeout=1.0)
                continue

        # Claim under the mutation lock, checking halt FIRST in the same locked
        # section. Every halt-set (retry exhaustion and fatal) also runs under
        # this lock, so the two are serialized: if a worker recorded exhaustion
        # before us, we observe halt here and never claim; if we claim first, the
        # task was decided before any halt existed. A task is therefore claimed
        # only when it will be started — never claimed-then-abandoned — so a run
        # never leaves an unattempted task in_progress to burn a retry.
        outcome = "claimed"  # "claimed" | "halted" | "race"
        with lock:
            if halt.is_set():
                outcome = "halted"
            else:
                try:
                    services.beads.claim_task(task.id)
                    _revert_beads_export(services)
                except PlanningError as exc:
                    services.emitter.emit(
                        "claim_race", task_id=task.id, detail=str(exc)
                    )
                    outcome = "race"
        if outcome != "claimed":
            release_slot(pool.name)
            if outcome == "halted":
                break
            continue  # claim race — another actor beat us; re-fetch and retry
        services.emitter.emit("task_claimed", task_id=task.id, title=task.title)

        thread = threading.Thread(
            target=run_task, args=(task, pool), daemon=True
        )
        thread.start()
        threads.append(thread)

    # Drain: let every in-flight worker reach its normal terminal, then surface
    # an unexpected fault first, else a retry-exhaustion halt.
    for thread in threads:
        thread.join()
    if fatal_error:
        raise fatal_error[0]
    if halt_error:
        raise halt_error[0]
