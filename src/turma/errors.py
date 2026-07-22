"""Shared exception types for the Turma CLI."""


class PlanningError(Exception):
    """Raised when planning fails."""


class SwarmHalted(PlanningError):
    """`turma run` halted on an exhausted retry budget — a controlled stop.

    A subclass of `PlanningError` so existing `except PlanningError` handlers
    keep working unchanged; the CLI catches it *specifically* to mark the
    `run_completed` outcome as `"halted"` (a deliberate halt) rather than
    `"error"` (an unexpected failure). See `run-v1-stream-identity`.
    """
