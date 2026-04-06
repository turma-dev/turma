"""Tests for turma config loading."""

import os
from pathlib import Path

import pytest

from turma.config import ConfigError, load_config


VALID_CONFIG = """\
[swarm]
max_parallel = 4

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
