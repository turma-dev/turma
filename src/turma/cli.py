"""CLI entry point for the Turma project."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from turma import __version__
from turma.errors import PlanningError
from turma.planning import run_planning
from turma.swarm import run_swarm, status_summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="turma",
        description="Provider-pool-aware multi-agent coding orchestration.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize local project scaffolding.")
    init_parser.add_argument(
        "--path",
        default=".",
        help="Project directory to initialize. Defaults to the current directory.",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing turma.toml.",
    )

    plan_parser = subparsers.add_parser("plan", help="Run the single-pass planning workflow.")
    plan_parser.add_argument("--feature", required=True, help="Feature name to plan.")

    run_parser = subparsers.add_parser("run", help="Run the implementation swarm scaffold.")
    run_parser.add_argument("--feature", help="Feature name to run.")

    subparsers.add_parser("status", help="Show current swarm status scaffold.")

    return parser


GITIGNORE_MANAGED = [
    "# Turma local state",
    "turma.toml",
    ".turma/",
    ".langgraph/",
    "*.task_complete",
    "*.task_progress",
]


def cmd_init(path: str, force: bool = False) -> int:
    project_path = Path(path).resolve()
    example = project_path / "turma.example.toml"
    target = project_path / "turma.toml"

    try:
        if not example.exists():
            print(f"error: {example} not found")
            return 1

        if target.exists() and not force:
            print("skipped turma.toml (already exists, use --force to overwrite)")
        else:
            shutil.copy2(example, target)
            print("created turma.toml from turma.example.toml")

        _update_gitignore(project_path)
    except OSError as exc:
        print(f"error: {exc}")
        return 1

    return 0


def _update_gitignore(project_path: Path) -> None:
    gitignore = project_path / ".gitignore"

    if gitignore.exists():
        existing = gitignore.read_text()
    else:
        existing = ""

    existing_lines = set(existing.splitlines())
    missing = [e for e in GITIGNORE_MANAGED if e not in existing_lines]
    missing_entries = [e for e in missing if not e.startswith("#")]

    if not missing_entries and GITIGNORE_MANAGED[0] in existing_lines:
        print("skipped .gitignore (all entries present)")
        return

    block = "\n".join(missing) + "\n"

    if existing and not existing.endswith("\n"):
        block = "\n" + block

    if existing:
        block = "\n" + block

    gitignore.write_text(existing + block)
    entry_count = len(missing_entries)
    if GITIGNORE_MANAGED[0] in missing and entry_count == 0:
        print("updated .gitignore (added header)")
    else:
        print(f"updated .gitignore (added {entry_count} entries)")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return cmd_init(args.path, force=args.force)
    if args.command == "plan":
        try:
            run_planning(args.feature)
            return 0
        except PlanningError as exc:
            print(f"error: {exc}")
            return 1
    if args.command == "run":
        print(run_swarm(args.feature))
        return 0
    if args.command == "status":
        print(status_summary())
        return 0

    parser.error("unknown command")
    return 2
