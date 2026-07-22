"""CLI entry point for the Turma project."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from turma import __version__
from turma.config import ConfigError, build_swarm_router, load_swarm_config
from turma.errors import PlanningError, SwarmHalted
from turma.planning import (
    default_planning_services,
    render_plan_snapshot,
    run_planning,
)
from turma.planning.resume import ResumeAction, ResumeRequest, resume_plan
from turma.swarm import (
    DEFAULT_WORKER_BACKEND,
    default_swarm_services,
    run_swarm,
    status_readout,
)
from turma.swarm.events import HeartbeatTicker, JsonEmitter
from turma.swarm.worker import registered_worker_backends
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
    plan_parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit a turma.plan.v1 JSON snapshot of the planning outcome "
            "instead of text (stdout is exactly one JSON document)."
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

    run_parser = subparsers.add_parser(
        "run",
        help="Run the swarm orchestrator for a feature.",
    )
    run_parser.add_argument(
        "--feature", required=True, help="Feature name to run."
    )
    run_parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Cap outer-loop iterations. Default: unbounded.",
    )
    run_parser.add_argument(
        "--backend",
        default=None,
        help=(
            "Worker backend name. Registered: "
            f"{', '.join(registered_worker_backends())}. "
            f"Default: {DEFAULT_WORKER_BACKEND}."
        ),
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run preflight + reconciliation only; no claims, no "
            "commits, no PRs."
        ),
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit a turma.run.v1 NDJSON event stream (one object per "
            "line) instead of text, for scripts and live surfaces."
        ),
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Show a read-only status readout for a feature.",
    )
    status_parser.add_argument(
        "--feature", required=True, help="Feature name to inspect."
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the readout as turma.status.v1 JSON instead of text.",
    )

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
                    quiet=args.json,
                )
                if args.json:
                    print(
                        render_plan_snapshot(
                            result, args.feature, action=request.action.value
                        )
                    )
                else:
                    _print_resume_result(request, result)
            else:
                _reject_stray_resume_flags(args)
                run_planning(args.feature, as_json=args.json)
            return 0
        except PlanningError as exc:
            if args.json:
                print(
                    json.dumps(
                        {"schema": "turma.plan.v1", "error": str(exc)},
                        indent=2,
                    )
                )
            else:
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
        # In --json mode every line — the terminal failure AND the run
        # lifecycle (run_started / run_completed / heartbeat) — is a
        # turma.run.v1 event. The lifecycle envelope + heartbeat are
        # --json-only, CLI-owned: they bookend the whole invocation (including
        # a pre-run config/services failure, where there is no services.emitter
        # yet), and the default text path runs the core sequence bare.
        emitter = JsonEmitter() if args.json else None
        started_at = time.monotonic()
        heartbeat: HeartbeatTicker | None = None
        run_outcome = "error"  # until a cleaner terminal is reached

        def _run_error(exc: Exception) -> int:
            if emitter is not None:
                emitter.emit("error", message=str(exc))
            else:
                print(f"error: {exc}")
            return 1

        try:
            config = load_swarm_config()
            # CLI flags take precedence over [swarm] in turma.toml; all other
            # knobs come from config since they have no flag.
            backend = args.backend or config.swarm.worker_backend
            # Route through the concurrent multi-pool dispatcher when the
            # operator asked for parallelism (max_parallel > 1) or declared
            # [[swarm.pools]]; otherwise keep the sequential loop. `--backend`
            # collapses routing to a single-backend pool. The default config
            # (max_parallel = 1, no pools) passes router=None — unchanged.
            use_concurrent = (
                config.swarm.max_parallel > 1 or bool(config.swarm.pools)
            )
            router = build_swarm_router(
                config.swarm, backend_override=args.backend
            )
            services = default_swarm_services(
                repo_root=Path.cwd(),
                backend=backend,
                base_branch=config.swarm.base_branch,
                max_retries=config.swarm.max_retries,
                worker_timeout=config.swarm.worker_timeout,
                worktree_root=config.swarm.worktree_root,
                emitter=emitter,
            )
            # Config + services built → the run actually starts.
            if emitter is not None:
                emitter.emit(
                    "run_started",
                    feature=args.feature,
                    dry_run=args.dry_run,
                    mode="concurrent" if use_concurrent else "sequential",
                    max_parallel=config.swarm.max_parallel,
                    **(
                        {
                            "pools": [
                                {"name": p.name, "backend": p.backend, "max": p.max}
                                for p in router.pools
                            ]
                        }
                        if use_concurrent
                        else {"backend": backend}
                    ),
                )
                heartbeat = HeartbeatTicker(
                    emitter, config.swarm.heartbeat_interval, started_at
                ).start()
            run_swarm(
                args.feature,
                services=services,
                max_tasks=args.max_tasks,
                backend=backend,
                dry_run=args.dry_run,
                router=router if use_concurrent else None,
                max_parallel=config.swarm.max_parallel,
            )
            run_outcome = "completed"
            return 0
        except SwarmHalted as exc:  # subclass of PlanningError — must precede it
            run_outcome = "halted"
            return _run_error(exc)
        except (ConfigError, PlanningError) as exc:
            return _run_error(exc)  # run_outcome stays "error"
        finally:
            if heartbeat is not None:
                heartbeat.stop()
            if emitter is not None:
                emitter.emit(
                    "run_completed",
                    outcome=run_outcome,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                )
    if args.command == "status":
        # `status --json` is a snapshot command, so a failure is a
        # structured error object (matching `plan --json`), not a bare
        # `error: <msg>` text line — keeping the JSON surface parseable.
        def _status_error(exc: Exception) -> int:
            if args.json:
                print(
                    json.dumps(
                        {"schema": "turma.status.v1", "error": str(exc)},
                        indent=2,
                    )
                )
            else:
                print(f"error: {exc}")
            return 1

        try:
            config = load_swarm_config()
        except ConfigError as exc:
            return _status_error(exc)
        try:
            services = default_swarm_services(
                repo_root=Path.cwd(),
                backend=config.swarm.worker_backend,
                base_branch=config.swarm.base_branch,
                max_retries=config.swarm.max_retries,
                worker_timeout=config.swarm.worker_timeout,
                worktree_root=config.swarm.worktree_root,
            )
            readout = status_readout(
                args.feature,
                services=services,
                repo_root=Path.cwd(),
                as_json=args.json,
            )
        except PlanningError as exc:
            return _status_error(exc)
        print(readout)
        return 0

    parser.error("unknown command")
    return 2
