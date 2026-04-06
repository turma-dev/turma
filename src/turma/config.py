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
class TurmaConfig:
    planning: PlanningConfig
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

    return TurmaConfig(planning=planning, raw=raw)
