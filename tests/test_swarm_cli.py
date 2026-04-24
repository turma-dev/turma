"""Tests for the `turma run` CLI subcommand."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from turma.cli import build_parser, main
from turma.errors import PlanningError


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


@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_happy_path_calls_run_swarm(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
) -> None:
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


@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_propagates_explicit_backend_to_both_calls(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
) -> None:
    mock_services_factory.return_value = MagicMock()
    main([
        "run", "--feature", "oauth", "--backend", "claude-code",
    ])
    # Both the factory and run_swarm see the explicit backend name.
    assert mock_services_factory.call_args.kwargs["backend"] == "claude-code"
    assert mock_run_swarm.call_args.kwargs["backend"] == "claude-code"


@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_planning_error_from_factory_exits_1(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing external CLI surfaces as a PlanningError at factory
    construction and must exit 1 before any Beads state is touched."""
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


@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_planning_error_from_run_swarm_exits_1(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A PlanningError raised mid-orchestration (e.g. preflight fail,
    budget exhausted) must exit 1 with `error: <msg>` on stdout."""
    mock_services_factory.return_value = MagicMock()
    mock_run_swarm.side_effect = PlanningError(
        "feature 'oauth' is not APPROVED"
    )

    exit_code = main(["run", "--feature", "oauth"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert out.startswith("error: ")
    assert "not APPROVED" in out


@patch("turma.cli.default_swarm_services")
@patch("turma.cli.run_swarm")
def test_main_run_defaults_backend_to_claude_code(
    mock_run_swarm: MagicMock,
    mock_services_factory: MagicMock,
) -> None:
    mock_services_factory.return_value = MagicMock()
    main(["run", "--feature", "oauth"])
    # Default backend flows through to both the factory and run_swarm.
    assert mock_services_factory.call_args.kwargs["backend"] == "claude-code"
    assert mock_run_swarm.call_args.kwargs["backend"] == "claude-code"
