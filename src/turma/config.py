"""Config loading for the Turma CLI."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """Raised when turma.toml is missing, malformed, or incomplete."""


@dataclass
class PlanningConfig:
    author_model: str
    critic_model: str = ""
    max_rounds: int = 4
    interactive: bool = True


@dataclass
class SwarmConfig:
    """`[swarm]` block in `turma.toml`. Consumed by `turma run`.

    Defaults mirror `default_swarm_services` in
    `src/turma/swarm/_orchestrator.py`; a turma.toml without a
    `[swarm]` block (or a partial block) produces the same defaults
    the CLI would apply if no config were loaded at all.
    """

    worker_backend: str = "claude-code"
    worker_timeout: int = 1800
    max_retries: int = 1
    worktree_root: str = ".worktrees"
    base_branch: str = "main"


@dataclass
class TurmaConfig:
    planning: PlanningConfig
    swarm: SwarmConfig = field(default_factory=SwarmConfig)
    raw: dict = field(default_factory=dict, repr=False)


def load_config() -> TurmaConfig:
    """Load turma.toml requiring a usable `[planning]` block.

    Used by `turma plan` / `turma plan-to-beads`, which consume
    `planning.author_model` / `planning.critic_model`. A missing
    `author_model` raises `ConfigError`. The `[swarm]` block is
    parsed the same way as `load_swarm_config`.
    """
    raw = _load_toml_dict()
    planning = _build_planning(raw.get("planning", {}), required=True)
    swarm = _parse_swarm(raw.get("swarm", {}))
    return TurmaConfig(planning=planning, swarm=swarm, raw=raw)


def load_swarm_config() -> TurmaConfig:
    """Load turma.toml for `turma run`.

    Does NOT require a `[planning]` block — the swarm orchestrator
    does not consume `planning.author_model`. A repo with a valid
    `[swarm]` block (or no config at all) but no planning section
    can still run the orchestrator against an already-transcribed
    feature. If `[planning]` is present but missing `author_model`,
    the returned `PlanningConfig` has `author_model=""` — callers
    that need planning config should use `load_config()` instead.
    """
    raw = _load_toml_dict()
    planning = _build_planning(raw.get("planning", {}), required=False)
    swarm = _parse_swarm(raw.get("swarm", {}))
    return TurmaConfig(planning=planning, swarm=swarm, raw=raw)


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


def _load_toml_dict() -> dict:
    """Read and decode `./turma.toml` into a dict.

    Shared by every entry point so the "missing" and "malformed"
    error surfaces are identical regardless of which command loads.
    """
    config_path = Path.cwd() / "turma.toml"
    if not config_path.exists():
        raise ConfigError(
            "turma.toml not found. Run `turma init` first."
        )
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"turma.toml is malformed: {exc}") from exc


def _build_planning(
    planning_raw: dict, *, required: bool
) -> PlanningConfig:
    """Translate the `[planning]` mapping into a `PlanningConfig`.

    With `required=True` (the `turma plan` / `turma plan-to-beads`
    path), missing `author_model` raises `ConfigError`. With
    `required=False` (the `turma run` path), a missing
    `author_model` produces an empty `PlanningConfig(author_model="")`
    so callers that don't consume planning config can proceed.
    """
    if "author_model" not in planning_raw:
        if required:
            raise ConfigError(
                "planning.author_model is required in turma.toml"
            )
        return PlanningConfig(author_model="")
    return PlanningConfig(
        author_model=planning_raw["author_model"],
        critic_model=planning_raw.get("critic_model", ""),
        max_rounds=planning_raw.get("max_rounds", 4),
        interactive=planning_raw.get("interactive", True),
    )


def _parse_swarm(swarm_raw: dict) -> SwarmConfig:
    """Parse `[swarm]` into a `SwarmConfig`, validating each key.

    Missing keys fall back to `SwarmConfig`'s defaults. Type /
    domain errors raise `ConfigError` with a pointer at the offending
    key so operators fix turma.toml rather than chasing a
    surfaced-elsewhere orchestrator failure.
    """
    defaults = SwarmConfig()

    worker_backend = swarm_raw.get("worker_backend", defaults.worker_backend)
    if not isinstance(worker_backend, str) or not worker_backend:
        raise ConfigError(
            "swarm.worker_backend must be a non-empty string"
        )

    worker_timeout = swarm_raw.get("worker_timeout", defaults.worker_timeout)
    if not isinstance(worker_timeout, int) or worker_timeout <= 0:
        raise ConfigError(
            "swarm.worker_timeout must be a positive integer (seconds)"
        )

    max_retries = swarm_raw.get("max_retries", defaults.max_retries)
    if not isinstance(max_retries, int) or max_retries < 0:
        raise ConfigError(
            "swarm.max_retries must be a non-negative integer"
        )

    worktree_root = swarm_raw.get("worktree_root", defaults.worktree_root)
    if not isinstance(worktree_root, str) or not worktree_root:
        raise ConfigError(
            "swarm.worktree_root must be a non-empty string"
        )

    base_branch = swarm_raw.get("base_branch", defaults.base_branch)
    if not isinstance(base_branch, str) or not base_branch:
        raise ConfigError(
            "swarm.base_branch must be a non-empty string"
        )

    return SwarmConfig(
        worker_backend=worker_backend,
        worker_timeout=worker_timeout,
        max_retries=max_retries,
        worktree_root=worktree_root,
        base_branch=base_branch,
    )
