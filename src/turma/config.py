"""Config loading for the Turma CLI."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from turma.errors import PlanningError
from turma.swarm.pools import Pool, PoolRouter, build_router
from turma.swarm.worker import registered_worker_backends


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
    # Concurrent multi-pool execution (swarm-parallel-multi-pool). Defaults
    # preserve today's sequential single-backend behavior: no pools + a global
    # cap of 1. `max_parallel` counts top-level worker slots.
    max_parallel: int = 1
    pools: tuple[Pool, ...] = ()


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

    max_parallel = swarm_raw.get("max_parallel", defaults.max_parallel)
    if (
        not isinstance(max_parallel, int)
        or isinstance(max_parallel, bool)
        or max_parallel < 1
    ):
        raise ConfigError("swarm.max_parallel must be a positive integer")

    pools = _parse_pools(swarm_raw.get("pools", []))

    return SwarmConfig(
        worker_backend=worker_backend,
        worker_timeout=worker_timeout,
        max_retries=max_retries,
        worktree_root=worktree_root,
        base_branch=base_branch,
        max_parallel=max_parallel,
        pools=pools,
    )


def _parse_pools(pools_raw: object) -> tuple[Pool, ...]:
    """Parse `[[swarm.pools]]` into validated `Pool`s.

    Per-key shape errors, an unknown backend (not in the worker registry), and
    the cross-pool rules (exactly one default, no duplicate task types across
    pools, `max >= 1`) raise `ConfigError` so the operator fixes turma.toml.
    The cross-pool rules reuse `build_router`.
    """
    if not isinstance(pools_raw, list):
        raise ConfigError(
            "swarm.pools must be an array of tables ([[swarm.pools]])"
        )
    if not pools_raw:
        return ()

    pools: list[Pool] = []
    for index, entry in enumerate(pools_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"swarm.pools[{index}] must be a table")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"swarm.pools[{index}].name must be a non-empty string"
            )
        backend = entry.get("backend")
        if not isinstance(backend, str) or not backend:
            raise ConfigError(
                f"swarm.pools[{name!r}].backend must be a non-empty string"
            )
        if backend not in registered_worker_backends():
            raise ConfigError(
                f"swarm.pools[{name!r}].backend: unknown worker backend "
                f"{backend!r}. Registered: "
                f"{list(registered_worker_backends())}"
            )
        types = entry.get("types", [])
        if not isinstance(types, list) or not all(
            isinstance(t, str) and t for t in types
        ):
            raise ConfigError(
                f"swarm.pools[{name!r}].types must be a list of "
                "non-empty strings"
            )
        max_slots = entry.get("max", 1)
        if not isinstance(max_slots, int) or isinstance(max_slots, bool):
            raise ConfigError(f"swarm.pools[{name!r}].max must be an integer")
        default = entry.get("default", False)
        if not isinstance(default, bool):
            raise ConfigError(
                f"swarm.pools[{name!r}].default must be a boolean"
            )
        pools.append(
            Pool(
                name=name,
                backend=backend,
                types=tuple(types),
                max=max_slots,
                default=default,
            )
        )

    try:
        build_router(pools)  # cross-pool validation only
    except PlanningError as exc:
        raise ConfigError(f"swarm.pools: {exc}") from exc
    return tuple(pools)


def build_swarm_router(
    config: SwarmConfig, *, backend_override: str | None = None
) -> PoolRouter:
    """Build the routing table for a run from a `SwarmConfig`.

    `--backend <id>` (``backend_override``) wins: a single pool over all types
    on that backend. Otherwise the configured `[[swarm.pools]]` are used; with
    no pools configured, one implicit default pool over all types from
    `worker_backend` reproduces today's single-backend behavior.
    """
    if backend_override is not None:
        return build_router([
            Pool(
                name=backend_override,
                backend=backend_override,
                types=(),
                max=config.max_parallel,
                default=True,
            )
        ])
    if config.pools:
        return build_router(list(config.pools))
    return build_router([
        Pool(
            name=config.worker_backend,
            backend=config.worker_backend,
            types=(),
            max=config.max_parallel,
            default=True,
        )
    ])
