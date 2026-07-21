"""Concurrent multi-pool dispatcher for ``turma run`` (parallel execution).

Task 1 of ``swarm-parallel-multi-pool`` pins the concurrency and
serialization invariants as failing tests against this entry point. The
body — bounded ``asyncio`` slots, per-pool semaphores, and the single
global mutation lock serializing every Beads-DB call and every shared-``.git``
worktree/branch operation, with drain-not-cancel failure handling — lands in
Tasks 3-4. See the change's ``design.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from turma.swarm.pools import PoolRouter

if TYPE_CHECKING:  # avoid a runtime import cycle with _orchestrator
    from turma.swarm._orchestrator import SwarmServices


def dispatch_concurrent(
    feature: str,
    services: "SwarmServices",
    *,
    router: PoolRouter,
    max_parallel: int,
) -> None:
    """Run ``feature``'s ready tasks concurrently across provider pools.

    Not implemented yet — Task 1 only pins the invariants. When implemented
    this keeps up to ``max_parallel`` top-level worker slots in flight,
    gates each by its pool's cap, serializes all shared-state mutation
    behind one lock, and drains (does not cancel) in-flight slots on a
    halting failure.
    """
    raise NotImplementedError(
        "concurrent multi-pool dispatch is not implemented yet "
        "(swarm-parallel-multi-pool Tasks 3-4); Task 1 only pins the invariants"
    )
