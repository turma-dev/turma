"""Provider-pool routing for the parallel multi-pool swarm.

A *pool* binds a worker backend to a set of task types with a concurrency
cap. The router maps a task's ``turma-type:`` value to its pool so the
concurrent dispatcher (``swarm-parallel-multi-pool``) can route work across
independent provider rate-limit pools.

This module is pure — validation and lookup only, no I/O. Config parsing
(``[[swarm.pools]]`` TOML into :class:`Pool` objects) and the concurrent
dispatcher that consumes the router land in later tasks of the change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from turma.errors import PlanningError


@dataclass(frozen=True)
class Pool:
    """One provider pool: a backend, the task types it serves, its cap.

    ``default`` marks the fallback pool for task types no pool claims
    explicitly. Exactly one pool in a configuration is the default.
    """

    name: str
    backend: str
    types: tuple[str, ...]
    max: int
    default: bool = False


class PoolRouter:
    """Resolved routing table: ``turma-type`` -> :class:`Pool`, with a default.

    Build via :func:`build_router`, which validates the pool set; do not
    construct directly.
    """

    def __init__(
        self,
        pools: Sequence[Pool],
        default: Pool,
        by_type: dict[str, Pool],
    ) -> None:
        self.pools: tuple[Pool, ...] = tuple(pools)
        self.default: Pool = default
        self._by_type: dict[str, Pool] = dict(by_type)

    def pool_for(self, turma_type: str) -> Pool:
        """Return the pool serving ``turma_type``, or the default pool."""
        return self._by_type.get(turma_type, self.default)


def build_router(pools: Sequence[Pool]) -> PoolRouter:
    """Validate ``pools`` and build a :class:`PoolRouter`.

    Raises :class:`~turma.errors.PlanningError` when the pool set is
    unroutable:

    - no pools are given;
    - a ``turma-type:`` value appears in more than one pool's ``types``
      (routing would be order-dependent);
    - the number of pools with ``default = true`` is not exactly one;
    - any pool's ``max`` is below 1.
    """
    pools = tuple(pools)
    if not pools:
        raise PlanningError("swarm pools: at least one pool is required")

    by_type: dict[str, Pool] = {}
    for pool in pools:
        if pool.max < 1:
            raise PlanningError(
                f"swarm pool {pool.name!r}: max must be >= 1, got {pool.max}"
            )
        for task_type in pool.types:
            existing = by_type.get(task_type)
            if existing is not None:
                raise PlanningError(
                    f"swarm pools: task type {task_type!r} is claimed by both "
                    f"{existing.name!r} and {pool.name!r}; a type may belong "
                    "to at most one pool"
                )
            by_type[task_type] = pool

    defaults = [pool for pool in pools if pool.default]
    if len(defaults) != 1:
        raise PlanningError(
            "swarm pools: exactly one pool must set default = true, found "
            f"{len(defaults)}"
        )

    return PoolRouter(pools=pools, default=defaults[0], by_type=by_type)
