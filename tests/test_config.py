"""Tests for turma config loading."""

import os
from pathlib import Path

import pytest

from turma.config import (
    ConfigError,
    SwarmConfig,
    build_swarm_router,
    load_config,
    load_swarm_config,
)
from turma.errors import PlanningError


VALID_CONFIG = """\
[swarm]
worker_backend = "claude-code"
worker_timeout = 1800
max_retries = 1
worktree_root = ".worktrees"
base_branch = "main"

[planning]
author_model = "claude-opus-4-6"
critic_model = "claude-sonnet-4-6"
max_rounds = 4
interactive = true
"""

MISSING_AUTHOR_MODEL = """\
[planning]
critic_model = "claude-sonnet-4-6"
"""

MALFORMED_TOML = """\
[planning
author_model = broken
"""

MINIMAL_CONFIG = """\
[planning]
author_model = "claude-opus-4-6"
"""


def test_loads_valid_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_config returns correct planning values from a valid turma.toml."""
    (tmp_path / "turma.toml").write_text(VALID_CONFIG)
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.planning.author_model == "claude-opus-4-6"
    assert config.planning.critic_model == "claude-sonnet-4-6"
    assert config.planning.max_rounds == 4
    assert config.planning.interactive is True


def test_loads_minimal_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_config works with only the required planning.author_model key."""
    (tmp_path / "turma.toml").write_text(MINIMAL_CONFIG)
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.planning.author_model == "claude-opus-4-6"


def test_fails_when_turma_toml_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_config raises ConfigError when turma.toml does not exist."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="turma.toml not found"):
        load_config()


def test_fails_when_author_model_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_config raises ConfigError when planning.author_model is absent."""
    (tmp_path / "turma.toml").write_text(MISSING_AUTHOR_MODEL)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="planning.author_model"):
        load_config()


def test_fails_on_malformed_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_config raises ConfigError on invalid TOML syntax."""
    (tmp_path / "turma.toml").write_text(MALFORMED_TOML)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError):
        load_config()


def test_ignores_unknown_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_config ignores sections it does not recognize."""
    config_text = VALID_CONFIG + "\n[some_future_section]\nfoo = 42\n"
    (tmp_path / "turma.toml").write_text(config_text)
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.planning.author_model == "claude-opus-4-6"


# ---------------------------------------------------------------------
# [swarm] block
# ---------------------------------------------------------------------


def test_loads_explicit_swarm_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_config returns the explicit [swarm] values."""
    (tmp_path / "turma.toml").write_text(VALID_CONFIG)
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.swarm.worker_backend == "claude-code"
    assert config.swarm.worker_timeout == 1800
    assert config.swarm.max_retries == 1
    assert config.swarm.worktree_root == ".worktrees"
    assert config.swarm.base_branch == "main"


def test_swarm_defaults_when_block_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turma.toml without a [swarm] block yields SwarmConfig defaults."""
    (tmp_path / "turma.toml").write_text(MINIMAL_CONFIG)
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.swarm == SwarmConfig()
    assert config.swarm.worker_backend == "claude-code"
    assert config.swarm.worker_timeout == 1800
    assert config.swarm.max_retries == 1
    assert config.swarm.worktree_root == ".worktrees"
    assert config.swarm.base_branch == "main"


def test_swarm_partial_block_fills_remaining_with_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A [swarm] block with only some keys defaults the rest."""
    text = (
        MINIMAL_CONFIG
        + '\n[swarm]\nmax_retries = 3\nbase_branch = "trunk"\n'
    )
    (tmp_path / "turma.toml").write_text(text)
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.swarm.max_retries == 3
    assert config.swarm.base_branch == "trunk"
    # Untouched keys stay at their defaults.
    assert config.swarm.worker_backend == "claude-code"
    assert config.swarm.worker_timeout == 1800
    assert config.swarm.worktree_root == ".worktrees"


def test_swarm_rejects_negative_max_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = MINIMAL_CONFIG + "\n[swarm]\nmax_retries = -1\n"
    (tmp_path / "turma.toml").write_text(text)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="max_retries"):
        load_config()


def test_swarm_rejects_non_positive_worker_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = MINIMAL_CONFIG + "\n[swarm]\nworker_timeout = 0\n"
    (tmp_path / "turma.toml").write_text(text)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="worker_timeout"):
        load_config()


def test_swarm_rejects_empty_worktree_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = MINIMAL_CONFIG + '\n[swarm]\nworktree_root = ""\n'
    (tmp_path / "turma.toml").write_text(text)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="worktree_root"):
        load_config()


def test_swarm_rejects_non_string_worker_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = MINIMAL_CONFIG + "\n[swarm]\nworker_backend = 42\n"
    (tmp_path / "turma.toml").write_text(text)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="worker_backend"):
        load_config()


# ---------------------------------------------------------------------
# load_swarm_config — turma run must not require [planning]
# ---------------------------------------------------------------------


SWARM_ONLY_CONFIG = """\
[swarm]
worker_backend = "claude-code"
max_retries = 2
base_branch = "trunk"
"""


NO_SECTIONS_CONFIG = ""


def test_load_swarm_config_works_without_planning_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo with only [swarm] and no planning config can still run the swarm."""
    (tmp_path / "turma.toml").write_text(SWARM_ONLY_CONFIG)
    monkeypatch.chdir(tmp_path)

    config = load_swarm_config()

    assert config.swarm.worker_backend == "claude-code"
    assert config.swarm.max_retries == 2
    assert config.swarm.base_branch == "trunk"
    # PlanningConfig is present but empty — nothing consumes it.
    assert config.planning.author_model == ""


def test_load_swarm_config_works_with_completely_empty_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty turma.toml yields SwarmConfig defaults; no planning error."""
    (tmp_path / "turma.toml").write_text(NO_SECTIONS_CONFIG)
    monkeypatch.chdir(tmp_path)

    config = load_swarm_config()

    assert config.swarm == SwarmConfig()
    assert config.planning.author_model == ""


def test_load_swarm_config_still_raises_on_bad_swarm_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[swarm] validation is applied regardless of which loader is used."""
    (tmp_path / "turma.toml").write_text(
        SWARM_ONLY_CONFIG + "worker_timeout = -5\n"
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="worker_timeout"):
        load_swarm_config()


def test_load_swarm_config_still_raises_on_missing_turma_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `turma init`-first requirement still applies to turma run."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="turma.toml not found"):
        load_swarm_config()


def test_load_config_still_requires_planning_author_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`load_config` (used by turma plan) must keep rejecting missing author_model."""
    (tmp_path / "turma.toml").write_text(SWARM_ONLY_CONFIG)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="planning.author_model"):
        load_config()


# ---------------------------------------------------------------------
# [[swarm.pools]] — pool config + router (swarm-parallel-multi-pool Task 2)
# ---------------------------------------------------------------------

POOLS_CONFIG = (
    MINIMAL_CONFIG
    + """
[swarm]
max_parallel = 4

[[swarm.pools]]
name = "anthropic"
backend = "claude-code"
types = ["impl", "spec"]
max = 2
default = true

[[swarm.pools]]
name = "openai"
backend = "codex"
types = ["test", "docs"]
max = 2
"""
)


def _load(tmp_path, monkeypatch, text):
    (tmp_path / "turma.toml").write_text(text)
    monkeypatch.chdir(tmp_path)
    return load_config()


def test_parses_swarm_pools(tmp_path, monkeypatch):
    """[[swarm.pools]] parses into SwarmConfig.pools with max_parallel."""
    swarm = _load(tmp_path, monkeypatch, POOLS_CONFIG).swarm
    assert swarm.max_parallel == 4
    assert [p.name for p in swarm.pools] == ["anthropic", "openai"]
    anthropic = swarm.pools[0]
    assert anthropic.backend == "claude-code"
    assert anthropic.types == ("impl", "spec")
    assert anthropic.max == 2 and anthropic.default is True


def test_max_parallel_defaults_to_one(tmp_path, monkeypatch):
    """No max_parallel key → 1 (today's sequential-equivalent default)."""
    assert _load(tmp_path, monkeypatch, MINIMAL_CONFIG).swarm.max_parallel == 1


def test_build_swarm_router_from_configured_pools(tmp_path, monkeypatch):
    """build_swarm_router routes turma-type → pool → backend."""
    router = build_swarm_router(_load(tmp_path, monkeypatch, POOLS_CONFIG).swarm)
    assert router.pool_for("impl").backend == "claude-code"
    assert router.pool_for("test").backend == "codex"
    # unmatched type → the default pool
    assert router.pool_for("chore").name == "anthropic"


def test_build_swarm_router_backcompat_single_implicit_pool(tmp_path, monkeypatch):
    """No [[swarm.pools]] → one implicit default pool from worker_backend that
    serves every type."""
    text = MINIMAL_CONFIG + '\n[swarm]\nworker_backend = "opencode"\n'
    router = build_swarm_router(_load(tmp_path, monkeypatch, text).swarm)
    assert router.pool_for("impl").backend == "opencode"
    assert router.pool_for("anything").backend == "opencode"


def test_build_swarm_router_backend_override(tmp_path, monkeypatch):
    """--backend override → a single pool over all types on that backend,
    ignoring any configured pools."""
    router = build_swarm_router(
        _load(tmp_path, monkeypatch, POOLS_CONFIG).swarm, backend_override="codex"
    )
    assert router.pool_for("impl").backend == "codex"
    assert router.pool_for("test").backend == "codex"


def test_duplicate_pool_types_is_config_error(tmp_path, monkeypatch):
    text = (
        MINIMAL_CONFIG
        + """
[[swarm.pools]]
name = "a"
backend = "claude-code"
types = ["impl"]
default = true

[[swarm.pools]]
name = "b"
backend = "codex"
types = ["impl"]
"""
    )
    with pytest.raises(ConfigError, match="at most one pool"):
        _load(tmp_path, monkeypatch, text)


def test_pools_require_exactly_one_default_is_config_error(tmp_path, monkeypatch):
    text = (
        MINIMAL_CONFIG
        + """
[[swarm.pools]]
name = "a"
backend = "claude-code"
types = ["impl"]

[[swarm.pools]]
name = "b"
backend = "codex"
types = ["test"]
"""
    )
    with pytest.raises(ConfigError, match="exactly one pool"):
        _load(tmp_path, monkeypatch, text)


def test_pool_missing_name_is_config_error(tmp_path, monkeypatch):
    text = (
        MINIMAL_CONFIG
        + '\n[[swarm.pools]]\nbackend = "claude-code"\ntypes = ["impl"]\ndefault = true\n'
    )
    with pytest.raises(ConfigError, match="name"):
        _load(tmp_path, monkeypatch, text)


def test_max_parallel_must_be_positive_is_config_error(tmp_path, monkeypatch):
    text = MINIMAL_CONFIG + "\n[swarm]\nmax_parallel = 0\n"
    with pytest.raises(ConfigError, match="max_parallel"):
        _load(tmp_path, monkeypatch, text)


def test_pool_unknown_backend_is_config_error(tmp_path, monkeypatch):
    """A [[swarm.pools]] backend not in the worker registry is rejected at
    load time (matching the --backend/worker_backend gate), not deferred to
    the dispatcher."""
    text = (
        MINIMAL_CONFIG
        + """
[[swarm.pools]]
name = "a"
backend = "typo"
types = ["impl"]
default = true
"""
    )
    with pytest.raises(ConfigError, match="unknown worker backend"):
        _load(tmp_path, monkeypatch, text)
