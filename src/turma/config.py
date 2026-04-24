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
    """Load turma.toml from the current working directory."""
    config_path = Path.cwd() / "turma.toml"

    if not config_path.exists():
        raise ConfigError(
            "turma.toml not found. Run `turma init` first."
        )

    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"turma.toml is malformed: {exc}") from exc

    planning_raw = raw.get("planning", {})

    if "author_model" not in planning_raw:
        raise ConfigError(
            "planning.author_model is required in turma.toml"
        )

    planning = PlanningConfig(
        author_model=planning_raw["author_model"],
        critic_model=planning_raw.get("critic_model", ""),
        max_rounds=planning_raw.get("max_rounds", 4),
        interactive=planning_raw.get("interactive", True),
    )

    swarm = _parse_swarm(raw.get("swarm", {}))

    return TurmaConfig(planning=planning, swarm=swarm, raw=raw)


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
