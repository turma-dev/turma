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
from turma.swarm.status import status_readout


__all__ = [
    "DEFAULT_WORKER_BACKEND",
    "SwarmServices",
    "default_swarm_services",
    "run_swarm",
    "status_readout",
]
