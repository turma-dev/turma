"""CLI entry point for the Turma project."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from turma import __version__
from turma.errors import PlanningError
from turma.planning import default_planning_services, run_planning
from turma.planning.resume import ResumeAction, ResumeRequest, resume_plan
from turma.swarm import run_swarm, status_summary
from turma.transcription import TranscriptionResult, transcribe_to_beads
from turma.transcription.beads import BeadsAdapter


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

    plan_parser = subparsers.add_parser("plan", help="Run the planning critic loop.")
    plan_parser.add_argument("--feature", required=True, help="Feature name to plan.")
    plan_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume a suspended plan for --feature instead of starting a new one.",
    )
    plan_parser.add_argument(
        "--approve",
        action="store_true",
        help="Approve the plan at the human gate (requires --resume).",
    )
    plan_parser.add_argument(
        "--revise",
        metavar="REASON",
        help="Send the plan back for revision with a reason (requires --resume).",
    )
    plan_parser.add_argument(
        "--abandon",
        metavar="REASON",
        help="Abandon the plan with a reason (requires --resume).",
    )
    plan_parser.add_argument(
        "--override",
        metavar="REASON",
        help=(
            "Override halted needs_human_review (requires --resume --approve)."
        ),
    )

    beads_parser = subparsers.add_parser(
        "plan-to-beads",
        help="Transcribe an approved plan into a feature-tagged Beads task set.",
    )
    beads_parser.add_argument(
        "--feature",
        required=True,
        help="Feature name whose openspec/changes/<feature>/ to transcribe.",
    )
    beads_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Tear down existing TRANSCRIBED.md-recorded or feature-tagged "
            "orphan Beads tasks before re-creating. Refuses when a prior "
            "TRANSCRIBED.md has no parseable task ids."
        ),
    )

    run_parser = subparsers.add_parser("run", help="Run the implementation swarm scaffold.")
    run_parser.add_argument("--feature", help="Feature name to run.")

    subparsers.add_parser("status", help="Show current swarm status scaffold.")

    return parser


def _build_resume_request(args: argparse.Namespace) -> ResumeRequest:
    """Parse the --resume action flags into a structured ResumeRequest."""
    primary_flags = [
        name
        for name, value in (
            ("approve", args.approve),
            ("revise", bool(args.revise)),
            ("abandon", bool(args.abandon)),
        )
        if value
    ]

    if len(primary_flags) > 1:
        raise PlanningError(
            "choose exactly one of --approve, --revise, --abandon"
        )

    if args.override and not args.approve:
        raise PlanningError("--override must be combined with --approve")
    if args.override and (args.revise or args.abandon):
        raise PlanningError(
            "--override may not be combined with --revise or --abandon"
        )

    if args.approve and args.override:
        return ResumeRequest(
            action=ResumeAction.OVERRIDE_APPROVE,
            reason=args.override,
        )
    if args.approve:
        return ResumeRequest(action=ResumeAction.APPROVE)
    if args.revise:
        return ResumeRequest(action=ResumeAction.REVISE, reason=args.revise)
    if args.abandon:
        return ResumeRequest(action=ResumeAction.ABANDON, reason=args.abandon)
    return ResumeRequest(action=ResumeAction.STATUS)


def _reject_stray_resume_flags(args: argparse.Namespace) -> None:
    """Reject resume-only flags when --resume is not set."""
    stray = [
        name
        for name, value in (
            ("--approve", args.approve),
            ("--revise", args.revise),
            ("--abandon", args.abandon),
            ("--override", args.override),
        )
        if value
    ]
    if stray:
        raise PlanningError(
            f"{', '.join(stray)} require --resume"
        )


def _print_resume_result(request: ResumeRequest, result) -> None:
    """Print a compact summary of the resume outcome."""
    state = result.state.get("state")
    print(f"action: {request.action.value}")
    print(f"state: {state}")
    if result.next_nodes:
        print(f"next: {', '.join(result.next_nodes)}")
    print(f"checkpoint: {result.checkpoint_path}")


def _print_transcription_result(result: TranscriptionResult) -> None:
    """Print a compact summary of the transcription outcome."""
    print(f"feature: {result.feature}")
    print(f"marker:  {result.transcribed_path}")
    print("tasks:")
    for num in sorted(result.ids_by_section):
        print(f"  section {num}: {result.ids_by_section[num]}")


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
            if args.resume:
                request = _build_resume_request(args)
                result = resume_plan(
                    args.feature,
                    default_planning_services(),
                    request,
                )
                _print_resume_result(request, result)
            else:
                _reject_stray_resume_flags(args)
                run_planning(args.feature)
            return 0
        except PlanningError as exc:
            print(f"error: {exc}")
            return 1
    if args.command == "plan-to-beads":
        try:
            adapter = BeadsAdapter()
            result = transcribe_to_beads(
                args.feature,
                adapter,
                force=args.force,
            )
            _print_transcription_result(result)
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
