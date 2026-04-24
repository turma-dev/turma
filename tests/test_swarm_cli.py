"""Tests for the `turma run` CLI subcommand."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from turma.cli import build_parser, main
from turma.config import SwarmConfig, TurmaConfig, PlanningConfig, ConfigError
from turma.errors import PlanningError


def _stub_config(swarm: SwarmConfig | None = None) -> TurmaConfig:
    """Build a `TurmaConfig` fixture for CLI tests that patch `load_config`."""
    return TurmaConfig(
        planning=PlanningConfig(author_model="claude-opus-4-6"),
        swarm=swarm or SwarmConfig(),
    )


# ---------------------------------------------------------------------
# Argparse surface
# ---------------------------------------------------------------------


def test_run_subparser_registered() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "--feature", "oauth"])
    assert args.command == "run"
    assert args.feature == "oauth"
    assert args.max_tasks is None
    assert args.backend is None
    assert args.dry_run is False


def test_run_accepts_full_flag_set() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "run",
        "--feature", "oauth",
        "--max-tasks", "3",
        "--backend", "claude-code",
        "--dry-run",
    ])
    assert args.feature == "oauth"
    assert args.max_tasks == 3
    assert args.backend == "claude-code"
    assert args.dry_run is True


def test_run_requires_feature(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run"])
    err = capsys.readouterr().err
    assert "--feature" in err


def test_run_rejects_unknown_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "run", "--feature", "oauth", "--nope",
        ])
    err = capsys.readouterr().err
    assert "--nope" in err or "unrecognized" in err


def test_run_max_tasks_rejects_non_int(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "run", "--feature", "oauth", "--max-tasks", "lots",
        ])
    err = capsys.readouterr().err
    assert "--max-tasks" in err or "invalid int" in err


# ---------------------------------------------------------------------
# Dispatch behavior
# ---------------------------------------------------------------------


@patch("turma.cli.load_swarm_config")
@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_happy_path_calls_run_swarm(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
    mock_load_swarm_config: MagicMock,
) -> None:
    mock_load_swarm_config.return_value = _stub_config()
    mock_services = MagicMock(name="SwarmServices")
    mock_services_factory.return_value = mock_services

    exit_code = main([
        "run",
        "--feature", "oauth",
        "--max-tasks", "2",
        "--dry-run",
    ])

    assert exit_code == 0
    # Factory called with default backend (no --backend passed).
    mock_services_factory.assert_called_once()
    kwargs = mock_services_factory.call_args.kwargs
    assert kwargs["backend"] == "claude-code"
    # run_swarm received the parsed args plus the constructed services.
    mock_run_swarm.assert_called_once()
    call = mock_run_swarm.call_args
    assert call.args == ("oauth",)
    assert call.kwargs["services"] is mock_services
    assert call.kwargs["max_tasks"] == 2
    assert call.kwargs["backend"] == "claude-code"
    assert call.kwargs["dry_run"] is True


@patch("turma.cli.load_swarm_config")
@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_propagates_explicit_backend_to_both_calls(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
    mock_load_swarm_config: MagicMock,
) -> None:
    mock_load_swarm_config.return_value = _stub_config()
    mock_services_factory.return_value = MagicMock()
    main([
        "run", "--feature", "oauth", "--backend", "claude-code",
    ])
    # Both the factory and run_swarm see the explicit backend name.
    assert mock_services_factory.call_args.kwargs["backend"] == "claude-code"
    assert mock_run_swarm.call_args.kwargs["backend"] == "claude-code"


@patch("turma.cli.load_swarm_config")
@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_planning_error_from_factory_exits_1(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
    mock_load_swarm_config: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing external CLI surfaces as a PlanningError at factory
    construction and must exit 1 before any Beads state is touched."""
    mock_load_swarm_config.return_value = _stub_config()
    mock_services_factory.side_effect = PlanningError(
        "bd CLI not found. Install it with `brew install beads`."
    )

    exit_code = main(["run", "--feature", "oauth"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert out.startswith("error: ")
    assert "bd CLI not found" in out
    # run_swarm never reached.
    mock_run_swarm.assert_not_called()


@patch("turma.cli.load_swarm_config")
@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_planning_error_from_run_swarm_exits_1(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
    mock_load_swarm_config: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A PlanningError raised mid-orchestration (e.g. preflight fail,
    budget exhausted) must exit 1 with `error: <msg>` on stdout."""
    mock_load_swarm_config.return_value = _stub_config()
    mock_services_factory.return_value = MagicMock()
    mock_run_swarm.side_effect = PlanningError(
        "feature 'oauth' is not APPROVED"
    )

    exit_code = main(["run", "--feature", "oauth"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert out.startswith("error: ")
    assert "not APPROVED" in out


@patch("turma.cli.load_swarm_config")
@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_defaults_backend_to_claude_code(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
    mock_load_swarm_config: MagicMock,
) -> None:
    mock_load_swarm_config.return_value = _stub_config()
    mock_services_factory.return_value = MagicMock()
    main(["run", "--feature", "oauth"])
    # Default backend flows through to both the factory and run_swarm.
    assert mock_services_factory.call_args.kwargs["backend"] == "claude-code"
    assert mock_run_swarm.call_args.kwargs["backend"] == "claude-code"


# ---------------------------------------------------------------------
# Config threading + CLI-over-config precedence
# ---------------------------------------------------------------------


@patch("turma.cli.load_swarm_config")
@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_threads_config_values_into_factory(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
    mock_load_swarm_config: MagicMock,
) -> None:
    """Non-default [swarm] values from turma.toml must reach the factory."""
    mock_load_swarm_config.return_value = _stub_config(
        SwarmConfig(
            worker_backend="claude-code",
            worker_timeout=600,
            max_retries=3,
            worktree_root=".cfg-worktrees",
            base_branch="trunk",
        )
    )
    mock_services_factory.return_value = MagicMock()

    main(["run", "--feature", "oauth"])

    kwargs = mock_services_factory.call_args.kwargs
    assert kwargs["backend"] == "claude-code"
    assert kwargs["worker_timeout"] == 600
    assert kwargs["max_retries"] == 3
    assert kwargs["worktree_root"] == ".cfg-worktrees"
    assert kwargs["base_branch"] == "trunk"


@patch("turma.cli.load_swarm_config")
@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_cli_backend_flag_overrides_config(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
    mock_load_swarm_config: MagicMock,
) -> None:
    """--backend beats [swarm].worker_backend from turma.toml."""
    mock_load_swarm_config.return_value = _stub_config(
        SwarmConfig(worker_backend="future-backend")
    )
    mock_services_factory.return_value = MagicMock()

    main(["run", "--feature", "oauth", "--backend", "claude-code"])

    assert (
        mock_services_factory.call_args.kwargs["backend"] == "claude-code"
    )
    assert mock_run_swarm.call_args.kwargs["backend"] == "claude-code"


@patch("turma.cli.load_swarm_config")
@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_backend_falls_back_to_config_when_flag_absent(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
    mock_load_swarm_config: MagicMock,
) -> None:
    """Without --backend, the factory/run_swarm get [swarm].worker_backend."""
    mock_load_swarm_config.return_value = _stub_config(
        SwarmConfig(worker_backend="claude-code")
    )
    mock_services_factory.return_value = MagicMock()

    main(["run", "--feature", "oauth"])

    assert (
        mock_services_factory.call_args.kwargs["backend"] == "claude-code"
    )
    assert mock_run_swarm.call_args.kwargs["backend"] == "claude-code"


@patch("turma.cli.load_swarm_config")
def test_main_run_config_error_exits_1(
    mock_load_swarm_config: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing / malformed turma.toml surfaces as exit 1 before the factory."""
    mock_load_swarm_config.side_effect = ConfigError(
        "turma.toml not found. Run `turma init` first."
    )
    exit_code = main(["run", "--feature", "oauth"])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert out.startswith("error: ")
    assert "turma.toml not found" in out


# ---------------------------------------------------------------------
# Command independence: `turma run` without [planning]
# ---------------------------------------------------------------------


@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_works_against_turma_toml_without_planning_section(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
    tmp_path,
    monkeypatch,
) -> None:
    """A turma.toml with only [swarm] and no [planning] block must not
    block `turma run`. End-to-end test against the real config loader
    (no load_swarm_config patch) to guard against future regressions
    where the loader picks up a planning requirement."""
    (tmp_path / "turma.toml").write_text(
        '[swarm]\nbase_branch = "trunk"\nmax_retries = 2\n'
    )
    monkeypatch.chdir(tmp_path)
    mock_services_factory.return_value = MagicMock()

    exit_code = main(["run", "--feature", "oauth"])

    assert exit_code == 0
    # Config values reached the factory.
    kwargs = mock_services_factory.call_args.kwargs
    assert kwargs["base_branch"] == "trunk"
    assert kwargs["max_retries"] == 2
    # Planning config was never required.
    mock_run_swarm.assert_called_once()
