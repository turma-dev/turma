"""Swarm orchestration for `turma run`.

v1 covers a single-feature sequential execution loop on top of the
Beads DAG produced by `turma plan-to-beads`. The orchestrator
(`run_swarm`) lives in `_orchestrator.py`; this module re-exports the
public surface so callers can keep doing `from turma.swarm import
run_swarm, SwarmServices`.
"""

from turma.swarm._orchestrator import (
    DEFAULT_WORKER_BACKEND,
    SwarmServices,
    default_swarm_services,
    run_swarm,
)


def status_summary() -> str:
    """Return a placeholder status message.

    Replaced by the real status summary in a later change set.
    """
    return (
        "Status scaffold. Integrate Beads, PR state, and reconciliation "
        "metadata."
    )


__all__ = [
    "DEFAULT_WORKER_BACKEND",
    "SwarmServices",
    "default_swarm_services",
    "run_swarm",
    "status_summary",
]
