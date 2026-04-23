"""Swarm orchestration for `turma run`.

v1 covers a single-feature sequential execution loop on top of the
Beads DAG produced by `turma plan-to-beads`. Implementation lands
task-by-task per `openspec/changes/swarm-orchestration/tasks.md`.

Until Task 7 wires the real orchestrator, `run_swarm` and
`status_summary` remain the placeholders the CLI has been calling
since before the spec landed. Task 7 replaces them with the actual
entry points.
"""


def run_swarm(feature: str | None) -> str:
    """Return a placeholder swarm message for the requested feature.

    Replaced by the real orchestrator entry in Task 7 of
    `openspec/changes/swarm-orchestration/tasks.md`.
    """
    if feature:
        return (
            f"Swarm scaffold for feature '{feature}'. "
            "Connect this command to the orchestrator described in "
            "docs/architecture.md."
        )
    return (
        "Swarm scaffold. Connect this command to the orchestrator "
        "described in docs/architecture.md."
    )


def status_summary() -> str:
    """Return a placeholder status message.

    Replaced by the real status summary in a later change set.
    """
    return (
        "Status scaffold. Integrate Beads, PR state, and reconciliation "
        "metadata."
    )
