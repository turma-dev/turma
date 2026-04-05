"""Tests for turma init command."""

from pathlib import Path

import pytest

from turma.cli import main


EXAMPLE_CONFIG = """\
[swarm]
max_parallel = 4
"""


def test_init_creates_turma_toml_from_example(tmp_path: Path) -> None:
    """turma init copies turma.example.toml to turma.toml."""
    example = tmp_path / "turma.example.toml"
    example.write_text(EXAMPLE_CONFIG)

    result = main(["init", "--path", str(tmp_path)])

    assert result == 0
    created = tmp_path / "turma.toml"
    assert created.exists()
    assert created.read_text() == EXAMPLE_CONFIG


def test_init_skips_existing_turma_toml(tmp_path: Path) -> None:
    """turma init does not overwrite existing turma.toml without --force."""
    example = tmp_path / "turma.example.toml"
    example.write_text(EXAMPLE_CONFIG)
    existing = tmp_path / "turma.toml"
    existing.write_text("# my custom config\n")

    result = main(["init", "--path", str(tmp_path)])

    assert result == 0
    assert existing.read_text() == "# my custom config\n"


def test_init_force_overwrites_turma_toml(tmp_path: Path) -> None:
    """turma init --force overwrites existing turma.toml."""
    example = tmp_path / "turma.example.toml"
    example.write_text(EXAMPLE_CONFIG)
    existing = tmp_path / "turma.toml"
    existing.write_text("# my custom config\n")

    result = main(["init", "--force", "--path", str(tmp_path)])

    assert result == 0
    assert existing.read_text() == EXAMPLE_CONFIG


def test_init_fails_without_example(tmp_path: Path) -> None:
    """turma init exits non-zero when turma.example.toml is missing."""
    result = main(["init", "--path", str(tmp_path)])
    assert result == 1


def test_init_creates_gitignore_when_missing(tmp_path: Path) -> None:
    """turma init creates .gitignore with turma entries if it does not exist."""
    example = tmp_path / "turma.example.toml"
    example.write_text(EXAMPLE_CONFIG)

    main(["init", "--path", str(tmp_path)])

    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    content = gitignore.read_text()
    assert "turma.toml" in content
    assert ".turma/" in content
    assert ".langgraph/" in content
    assert "*.task_complete" in content
    assert "*.task_progress" in content


def test_init_appends_to_existing_gitignore(tmp_path: Path) -> None:
    """turma init appends missing entries to an existing .gitignore."""
    example = tmp_path / "turma.example.toml"
    example.write_text(EXAMPLE_CONFIG)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules/\n.env\n")

    main(["init", "--path", str(tmp_path)])

    content = gitignore.read_text()
    assert content.startswith("node_modules/\n.env\n")
    assert "turma.toml" in content
    assert ".turma/" in content


def test_init_does_not_duplicate_gitignore_entries(tmp_path: Path) -> None:
    """turma init does not add entries already present in .gitignore."""
    example = tmp_path / "turma.example.toml"
    example.write_text(EXAMPLE_CONFIG)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("turma.toml\n.langgraph/\n")

    main(["init", "--path", str(tmp_path)])

    content = gitignore.read_text()
    assert content.count("turma.toml") == 1
    assert content.count(".langgraph/") == 1


def test_init_does_not_duplicate_header(tmp_path: Path) -> None:
    """turma init does not duplicate the managed block header."""
    example = tmp_path / "turma.example.toml"
    example.write_text(EXAMPLE_CONFIG)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("# Turma local state\nturma.toml\n")

    main(["init", "--path", str(tmp_path)])

    content = gitignore.read_text()
    assert content.count("# Turma local state") == 1


def test_init_idempotent(tmp_path: Path) -> None:
    """Running turma init twice changes nothing on the second run."""
    example = tmp_path / "turma.example.toml"
    example.write_text(EXAMPLE_CONFIG)

    main(["init", "--path", str(tmp_path)])
    toml_content = (tmp_path / "turma.toml").read_text()
    gitignore_content = (tmp_path / ".gitignore").read_text()

    result = main(["init", "--path", str(tmp_path)])

    assert result == 0
    assert (tmp_path / "turma.toml").read_text() == toml_content
    assert (tmp_path / ".gitignore").read_text() == gitignore_content


def test_init_reports_created(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """turma init reports what it created."""
    example = tmp_path / "turma.example.toml"
    example.write_text(EXAMPLE_CONFIG)

    main(["init", "--path", str(tmp_path)])

    output = capsys.readouterr().out
    assert "created turma.toml" in output
    assert "updated .gitignore" in output


def test_init_reports_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """turma init reports what it skipped on second run."""
    example = tmp_path / "turma.example.toml"
    example.write_text(EXAMPLE_CONFIG)

    main(["init", "--path", str(tmp_path)])
    capsys.readouterr()

    main(["init", "--path", str(tmp_path)])

    output = capsys.readouterr().out
    assert "skipped turma.toml" in output
    assert "skipped .gitignore" in output


def test_init_reports_error_on_missing_example(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """turma init reports a clear error when the example template is missing."""
    result = main(["init", "--path", str(tmp_path)])

    assert result == 1
    output = capsys.readouterr().out
    assert "error:" in output
    assert "turma.example.toml" in output


def test_init_returns_1_on_write_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """turma init returns 1 on filesystem write failure."""
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    example = readonly / "turma.example.toml"
    example.write_text(EXAMPLE_CONFIG)
    readonly.chmod(0o444)

    try:
        result = main(["init", "--force", "--path", str(readonly)])

        assert result == 1
        output = capsys.readouterr().out
        assert "error:" in output
    finally:
        readonly.chmod(0o755)
