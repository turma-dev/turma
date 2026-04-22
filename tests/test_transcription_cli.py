"""Tests for the `turma plan-to-beads` CLI subcommand."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from turma.cli import build_parser, main
from turma.errors import PlanningError
from turma.transcription import TranscriptionResult


# ---------------------------------------------------------------------
# Argparse surface
# ---------------------------------------------------------------------


def test_plan_to_beads_subparser_registered() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "plan-to-beads", "--feature", "oauth",
    ])
    assert args.command == "plan-to-beads"
    assert args.feature == "oauth"
    assert args.force is False


def test_plan_to_beads_force_flag_parses() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "plan-to-beads", "--feature", "oauth", "--force",
    ])
    assert args.force is True


def test_plan_to_beads_requires_feature(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["plan-to-beads"])
    err = capsys.readouterr().err
    assert "--feature" in err


# ---------------------------------------------------------------------
# Dispatch behavior
# ---------------------------------------------------------------------


@patch("turma.cli.BeadsAdapter")
@patch("turma.cli.transcribe_to_beads")
def test_main_dispatches_plan_to_beads_and_prints_result(
    mock_transcribe: MagicMock,
    mock_adapter_cls: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_transcribe.return_value = TranscriptionResult(
        feature="oauth",
        ids_by_section={1: "bd-1", 2: "bd-2"},
        transcribed_path=tmp_path / "TRANSCRIBED.md",
    )

    exit_code = main(["plan-to-beads", "--feature", "oauth"])

    assert exit_code == 0

    # Adapter was constructed and threaded through.
    mock_adapter_cls.assert_called_once_with()
    mock_transcribe.assert_called_once_with(
        "oauth",
        mock_adapter_cls.return_value,
        force=False,
    )

    out = capsys.readouterr().out
    assert "feature: oauth" in out
    assert "section 1: bd-1" in out
    assert "section 2: bd-2" in out
    assert str(tmp_path / "TRANSCRIBED.md") in out


@patch("turma.cli.BeadsAdapter")
@patch("turma.cli.transcribe_to_beads")
def test_main_forwards_force_flag(
    mock_transcribe: MagicMock,
    mock_adapter_cls: MagicMock,
    tmp_path: Path,
) -> None:
    mock_transcribe.return_value = TranscriptionResult(
        feature="oauth",
        ids_by_section={},
        transcribed_path=tmp_path / "TRANSCRIBED.md",
    )

    exit_code = main(["plan-to-beads", "--feature", "oauth", "--force"])

    assert exit_code == 0
    mock_transcribe.assert_called_once_with(
        "oauth",
        mock_adapter_cls.return_value,
        force=True,
    )


# ---------------------------------------------------------------------
# Error mapping (PlanningError → exit 1 with message printed)
# ---------------------------------------------------------------------


@patch("turma.cli.BeadsAdapter")
@patch("turma.cli.transcribe_to_beads")
def test_planning_error_from_pipeline_becomes_exit_1(
    mock_transcribe: MagicMock,
    mock_adapter_cls: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_transcribe.side_effect = PlanningError(
        "plan-to-beads requires the plan to be approved first"
    )

    exit_code = main(["plan-to-beads", "--feature", "oauth"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "error: plan-to-beads requires the plan to be approved" in out


@patch(
    "turma.cli.BeadsAdapter",
    side_effect=PlanningError(
        "bd CLI not found. Install it with `brew install beads`"
    ),
)
def test_missing_bd_cli_becomes_exit_1_with_install_hint(
    _mock_adapter_cls: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["plan-to-beads", "--feature", "oauth"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "brew install beads" in out


@patch("turma.cli.BeadsAdapter")
@patch("turma.cli.transcribe_to_beads")
def test_orphan_preflight_error_surfaces_in_output(
    mock_transcribe: MagicMock,
    mock_adapter_cls: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_transcribe.side_effect = PlanningError(
        "feature-tagged tasks already exist in Beads from a prior "
        "failed transcription (ids: bd-99, bd-100). Close them with "
        "`bd close bd-99 bd-100` or retry with --force."
    )

    exit_code = main(["plan-to-beads", "--feature", "oauth"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "bd-99" in out
    assert "--force" in out
