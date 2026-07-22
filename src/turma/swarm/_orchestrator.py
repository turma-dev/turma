"""Swarm orchestrator for `turma run` — single-feature sequential loop.

Drives one Beads task at a time from `ready` to `closed` (or `failed`
with a retry-budget decision). Each iteration follows the committed
state-machine contract in
`openspec/changes/swarm-orchestration/design.md`:

    preflight → reconcile (read-only) → repair_phase → main_loop

The module is strictly adapter-driven — every external effect
(bd / git / gh / worker CLI) goes through `SwarmServices`. Tests
inject stubs directly and assert on the call sequence; there is no
live subprocess in this module's unit-test scope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from turma.errors import PlanningError, SwarmHalted
from turma.swarm.dispatch import dispatch_concurrent
from turma.swarm.events import JsonEmitter, RunEmitter, TextEmitter
from turma.swarm.git import COMMIT_MESSAGE_TEMPLATE, GitAdapter
from turma.swarm.pools import PoolRouter
from turma.swarm.pull_request import PullRequestAdapter
from turma.swarm.worker import get_worker_backend
from turma.swarm.worktree import WorktreeManager, WorktreeRef
from turma.swarm.reconciliation import (
    CompletionPending,
    CompletionPendingWithPr,
    FailurePending,
    MissingWorktree,
    OrphanBranch,
    ReconciliationReport,
    StaleNoSentinels,
    reconcile_feature,
)
from turma.swarm.worker import (
    TASK_COMPLETE_SENTINEL,
    TASK_FAILED_SENTINEL,
    WorkerBackend,
    WorkerInvocation,
    registered_worker_backends,
)
from turma.transcription.beads import (
    BeadsAdapter,
    BeadsTaskRef,
    _extract_pr_number,
)


CLEAN_TREE_REASON = "worker reported success but left the tree clean"
TURMA_TYPE_LABEL_PREFIX = "turma-type:"
_DEFAULT_TURMA_TYPE = "impl"

# `gh pr create` returns the PR URL on its own line on success; the
# orchestrator records the PR's number on the bd task via
# `mark_pr_open` so the merge-advancement sweep can look it up
# directly. Pinned to GitHub's canonical PR URL shape; if `gh` ever
# returns a non-canonical form, `_pr_number_from_url` raises
# `PlanningError` rather than silently misrecording.
_PR_URL_PATTERN = re.compile(
    r"^https://github\.com/[^/]+/[^/]+/pull/(\d+)/?$"
)


def _pr_number_from_url(url: str) -> int:
    """Parse a GitHub PR URL into its integer PR number.

    `gh pr create` returns URLs of the canonical form
    `https://github.com/<owner>/<repo>/pull/<N>` (with an optional
    trailing slash). The orchestrator's success path depends on
    extracting `<N>` from that URL so it can label the bd task
    via `mark_pr_open(task_id, N)`. Raises `PlanningError` on
    URLs that don't match the canonical pattern — internal
    contract violation, halt rather than guess.
    """
    match = _PR_URL_PATTERN.match(url)
    if match is None:
        raise PlanningError(
            f"Could not parse PR number from URL: {url!r}. "
            "Expected `https://github.com/<owner>/<repo>/pull/<N>`."
        )
    return int(match.group(1))


# ---------------------------------------------------------------------
# SwarmServices — DI container
# ---------------------------------------------------------------------


DEFAULT_WORKER_BACKEND = "claude-code"


@dataclass
class SwarmServices:
    """Dependency-injection boundary for the swarm orchestrator.

    Mirrors the `PlanningServices` / transcription shapes. Tests pass
    stubs directly; the CLI (Task 8) constructs the real adapters.
    """

    beads: BeadsAdapter
    worktree: WorktreeManager
    git: GitAdapter
    pr: PullRequestAdapter
    worker_factory: Callable[[], WorkerBackend]
    repo_root: Path
    base_branch: str = "main"
    max_retries: int = 1
    worker_timeout: int = 1800
    # Run-event sink. Defaults to text (the historical print output);
    # the CLI swaps in a JsonEmitter for `turma run --json`.
    emitter: RunEmitter = field(default_factory=TextEmitter)
    # Backend-keyed worker resolver for the concurrent multi-pool dispatcher
    # (pool.backend name -> WorkerBackend). None on the sequential path, which
    # uses `worker_factory`. See `swarm-parallel-multi-pool`.
    worker_for: Callable[[str], WorkerBackend] | None = None


def default_swarm_services(
    repo_root: Path,
    *,
    backend: str = DEFAULT_WORKER_BACKEND,
    base_branch: str = "main",
    max_retries: int = 1,
    worker_timeout: int = 1800,
    worktree_root: str = ".worktrees",
    emitter: RunEmitter | None = None,
) -> SwarmServices:
    """Construct production `SwarmServices` rooted at `repo_root`.

    Each adapter preflights its CLI dependency at construction — `bd`
    for Beads, `git` for worktree + git operations, `gh` (plus an
    authenticated session via `gh auth status`) for the PR adapter.
    A missing or misconfigured dependency surfaces as a
    `PlanningError` here so the CLI can exit 1 before any Beads
    state is touched.

    The worker backend is resolved lazily via
    `get_worker_backend(backend)`: the `claude` CLI check only runs
    when the orchestrator actually claims a task and instantiates a
    worker, so `--dry-run` does not require Claude Code to be
    installed.
    """
    return SwarmServices(
        beads=BeadsAdapter(),
        worktree=WorktreeManager(
            repo_root=repo_root, worktree_root=worktree_root
        ),
        git=GitAdapter(),
        pr=PullRequestAdapter(),
        worker_factory=lambda: get_worker_backend(backend),
        # Backend-keyed resolver for the concurrent dispatcher: a pool's
        # `backend` name -> WorkerBackend. Harmless on the sequential path
        # (which uses `worker_factory`); lazy, so no CLI is probed until a
        # worker actually runs.
        worker_for=get_worker_backend,
        repo_root=repo_root,
        base_branch=base_branch,
        max_retries=max_retries,
        worker_timeout=worker_timeout,
        emitter=emitter if emitter is not None else TextEmitter(),
    )


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------


def run_swarm(
    feature: str,
    *,
    services: SwarmServices | None = None,
    max_tasks: int | None = None,
    backend: str | None = None,
    dry_run: bool = False,
    router: PoolRouter | None = None,
    max_parallel: int = 1,
) -> None:
    """Run the single-feature swarm for `feature`.

    - `services` is required for orchestration. Callers that invoke
      `run_swarm` programmatically must provide a `SwarmServices`.
    - `max_tasks` caps sequential outer-loop iterations (default
      unbounded). Not supported alongside `router` (parallel execution)
      unless `dry_run` — a dry-run exits before the loop, so the cap is
      moot and the preview stays usable.
    - `backend` is validated against the worker registry; it selects the
      single-backend for the sequential path and is informational for the
      concurrent path (whose per-pool backends come from `router`).
    - `dry_run=True` runs preflight + reconciliation only, exiting before
      the repair phase and the execution loop — on either path.
    - `router` selects execution: when provided, the concurrent multi-pool
      `dispatch_concurrent` owns the run (with `max_parallel` as the global
      slot cap); when None, the sequential `_main_loop` runs. The CLI builds
      a router exactly when `max_parallel > 1` or `[[swarm.pools]]` are
      configured, so the default configuration stays on the sequential path.
    """
    if services is None:
        raise PlanningError(
            "run_swarm requires a SwarmServices instance. The CLI "
            "wires default services in Task 8 of "
            "openspec/changes/swarm-orchestration/tasks.md."
        )
    if backend is not None and backend not in registered_worker_backends():
        raise PlanningError(
            f"unknown worker backend: {backend!r}. "
            f"Registered: {list(registered_worker_backends())}"
        )
    # --max-tasks has no defined semantics under parallel execution yet, so
    # refuse it up front rather than silently ignoring it. Only when we would
    # actually dispatch concurrently: --dry-run exits before the loop (below),
    # so a pooled `--dry-run --max-tasks N` preview stays usable.
    if router is not None and max_tasks is not None and not dry_run:
        raise PlanningError(
            "--max-tasks is not supported with parallel execution "
            "(max_parallel > 1 or configured [[swarm.pools]]). Rerun with "
            "max_parallel = 1 and no pools, or drop --max-tasks."
        )

    _preflight(feature, services.repo_root)

    # bd export.interval=0 is a Turma contract — see
    # `swarm-worker-commit-bd-ownership`. The default 60s throttle
    # defers exports to the next bd command (read or write), so any
    # operator-side `bd list --json` between iterations re-dirties
    # `.beads/issues.jsonl` and the next bd-state-clean preflight
    # refuses. Verifying this knob FIRST surfaces the operator's
    # config gap before the bd-state-clean preflight blames the
    # symptom. Skipped under --dry-run.
    if not dry_run:
        _preflight_bd_export_interval(services)

    # Refuse to start if `.beads/issues.jsonl` is already dirty
    # in main's working tree. Turma's revert-after-mutation
    # invariant (see swarm-beads-state-merge-cleanliness)
    # only holds from a clean baseline; pre-existing operator
    # changes must be triaged before Turma takes ownership of
    # the file's working-tree state. Skipped under --dry-run
    # since dry-run doesn't mutate bd state.
    if not dry_run:
        _preflight_beads_state_clean(services)

    # Fast-forward local <base_branch> from origin once per
    # invocation so dependent tasks claim against the merged base.
    # Skipped under --dry-run because the FF mutates a local ref.
    # See `openspec/changes/swarm-merge-advancement-stabilization/
    # design.md` "GitAdapter.fetch_and_ff_base" for the contract
    # (single colon-form fetch; refuses divergent local).
    if dry_run:
        services.emitter.emit("fetch_skipped")
    else:
        services.git.fetch_and_ff_base(
            services.repo_root, services.base_branch
        )
        services.emitter.emit(
            "fetch_advanced", base_branch=services.base_branch
        )

    report = reconcile_feature(
        feature,
        adapter=services.beads,
        worktree_manager=services.worktree,
        git_adapter=services.git,
        pr_adapter=services.pr,
        repo_root=services.repo_root,
        emitter=services.emitter,
    )

    if dry_run:
        # Dry-run preview: also surface what merge-advancement
        # would do without committing. Reads PR state but never
        # mutates Beads / worktree state. Repair-phase mutations
        # remain skipped on dry-run as before.
        _advance_merged_prs(feature, services, dry_run=True)
        return

    _apply_repairs(feature, report, services)
    _advance_merged_prs(feature, services, dry_run=False)
    if router is not None:
        dispatch_concurrent(
            feature, services, router=router, max_parallel=max_parallel
        )
    else:
        _main_loop(feature, services, max_tasks)


# ---------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------


def _preflight(feature: str, repo_root: Path) -> None:
    """Verify spec + approval + transcription artifacts exist.

    Pointed error messages tell the operator which prior `turma`
    command is missing so they can resume at the right point.
    """
    change_dir = repo_root / "openspec" / "changes" / feature
    if not change_dir.is_dir():
        raise PlanningError(
            f"no OpenSpec change directory for feature {feature!r} at "
            f"{change_dir}. Run `turma plan --feature {feature}` first."
        )
    if not (change_dir / "APPROVED").exists():
        raise PlanningError(
            f"feature {feature!r} is not APPROVED. Run "
            f"`turma plan --feature {feature}` and complete the "
            "author/critic loop."
        )
    if not (change_dir / "TRANSCRIBED.md").exists():
        raise PlanningError(
            f"feature {feature!r} has not been transcribed to Beads. "
            f"Run `turma plan-to-beads --feature {feature}` first."
        )


_BEADS_EXPORT_PATH = ".beads/issues.jsonl"
_BEADS_EXPORT = (_BEADS_EXPORT_PATH,)


def _revert_beads_export(services: SwarmServices) -> None:
    """Revert main's working-tree state of `.beads/issues.jsonl`
    after a Turma-initiated bd update.

    bd's `export.auto=true` + `export.git-add=true` (defaults)
    mean every `bd update` writes AND stages the file. The dolt
    db (the source of truth for bd state) keeps the mutation;
    this revert disposes of the export-file dirtiness so main's
    working tree stays clean for the next iteration's
    `fetch_and_ff_base`. bd's own Dolt-over-git auto-sync
    propagates the mutation to origin (see
    `swarm-bd-export-serialization`); the export file itself is a
    regenerable backup, not the propagation path, so there is no
    end-of-run "unpropagated" state to warn about.

    See `openspec/changes/swarm-beads-state-merge-cleanliness/
    design.md` "Adapter contract" for why this targets
    `restore --staged --worktree` (NOT `git checkout --`) and
    why the revert is sound (dolt remains canonical; the
    export is regenerable).
    """
    services.git.revert_paths(services.repo_root, _BEADS_EXPORT)


_BEADS_CONFIG_VALUE_PATTERN = re.compile(r"^([\w.-]+)\s*:\s*(.*)$")


def _read_beads_config_value(repo_root: Path, key: str) -> str:
    """Read a top-level scalar from `.beads/config.yaml` directly,
    without spawning bd.

    Why this exists: every bd subprocess startup runs an auto-
    export-on-startup check that re-writes `.beads/issues.jsonl`
    whenever it thinks the on-disk file is out of sync with
    dolt. Turma's revert-after-mutation contract from
    `swarm-beads-state-merge-cleanliness` leaves the file in
    exactly that out-of-sync state at the end of each run, so
    *any* bd subprocess Turma fires before the bd-state-clean
    preflight (including a read-only `bd config get`) trips bd's
    auto-export and breaks the preflight on the very next
    iteration. Reading `.beads/config.yaml` directly keeps bd
    out of the smoke-critical path. This was identified in the
    Task-6 follow-up via writer-attribution traces against the
    bd 1.0.2 reproducer.

    bd writes `config.yaml` as a flat top-level mapping with
    dotted keys (e.g. `export.interval: 0`). Comment lines
    (`#`-prefixed) are ignored. Surrounding double or single
    quotes on the value are stripped.

    Returns the raw stripped value, or empty string if the key
    is not present in the file. Raises `PlanningError` if the
    file does not exist (bd not initialized).
    """
    config_path = repo_root / ".beads" / "config.yaml"
    if not config_path.exists():
        raise PlanningError(
            f"{config_path} does not exist. Run `bd init` in the "
            "repo root before invoking `turma run`."
        )
    for line in config_path.read_text().splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _BEADS_CONFIG_VALUE_PATTERN.match(stripped)
        if match is None:
            continue
        if match.group(1) != key:
            continue
        value = match.group(2).strip()
        # Strip a single layer of surrounding matching quotes,
        # consistent with how YAML scalars are commonly written.
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ('"', "'")
        ):
            value = value[1:-1]
        return value
    return ""


def _preflight_bd_export_interval(services: SwarmServices) -> None:
    """Refuse to start if bd's `export.interval` is not 0.

    bd's default `export.interval=60` (seconds) defers auto-
    exports across bd commands; the next bd command (read OR
    write) flushes the deferred export and dirties
    `.beads/issues.jsonl`. With Turma's revert-after-mutation
    contract in place, that dirtying surfaces as a refused
    bd-state-clean preflight at the start of the NEXT run —
    blaming the operator for a config gap they may not realize
    they have.

    Pinning the contract here means operators get a clear
    message naming the exact knob and the exact remediation
    command. The setting persists in `.beads/config.yaml` and
    is committed alongside the project's bd setup.

    The check reads `.beads/config.yaml` directly rather than
    invoking `bd config get`. See `_read_beads_config_value` for
    why; in short, every bd subprocess startup fires bd's auto-
    export-on-startup writer, which would itself dirty the next
    preflight.

    See `openspec/changes/swarm-worker-commit-bd-ownership/`
    for the full rationale and the empirical model behind the
    knob's behavior.
    """
    value = _read_beads_config_value(
        services.repo_root, "export.interval"
    )
    if value != "0":
        observed = value or "(unset)"
        raise PlanningError(
            "turma run requires `bd config get export.interval` "
            f"to return 0; observed: {observed}. The default 60s "
            "throttle defers bd's auto-export across iterations. "
            "Any bd command between turma runs (including reads "
            "like `bd list --json`) flushes the deferred export "
            "and re-dirties .beads/issues.jsonl, causing the next "
            "preflight to refuse.\n"
            "Run:\n"
            "  bd config set export.interval 0\n"
            "The setting persists in .beads/config.yaml."
        )


def _preflight_beads_state_clean(services: SwarmServices) -> None:
    """Refuse to start if Turma's owned bd-state file is
    already dirty in main's working tree.

    Turma's revert-after-mutation invariant (see
    `openspec/changes/swarm-beads-state-merge-cleanliness/`)
    only holds from a clean baseline. If the operator has
    pre-existing uncommitted changes (from a manual `bd
    update`, a crashed prior `turma run`, or hand edits),
    Turma must NOT silently revert them.

    The message names the file and gives the operator
    triage commands so they can choose between committing,
    stashing, or discarding the pre-existing changes.

    Skipped under `--dry-run` (dry-run doesn't mutate bd
    state, so a dirty bd-state file is safe to ignore for
    a readout).
    """
    if services.git.path_is_dirty(
        services.repo_root, _BEADS_EXPORT_PATH
    ):
        raise PlanningError(
            f"{_BEADS_EXPORT_PATH} has uncommitted changes "
            "in main's working tree. turma run requires this "
            "file to be clean before starting because it "
            "manages the file's working-tree state across "
            "iterations. Triage with:\n"
            f"  git diff --cached {_BEADS_EXPORT_PATH}    # staged\n"
            f"  git diff {_BEADS_EXPORT_PATH}             # unstaged\n"
            f"  git stash push -- {_BEADS_EXPORT_PATH}    # save aside\n"
            f"  git restore --staged --worktree -- {_BEADS_EXPORT_PATH}    # discard"
        )


# ---------------------------------------------------------------------
# Repair phase
# ---------------------------------------------------------------------


def _apply_repairs(
    feature: str,
    report: ReconciliationReport,
    services: SwarmServices,
) -> None:
    """Apply the repair documented for each finding, in order.

    Halts before the main loop when:

    - any `stale-no-sentinels` finding is present (v1 never guesses
      on ambiguous state), or
    - any finding that calls `fail_task` exhausts the retry budget
      — repair-phase exhaustions must halt just like main-loop
      exhaustions (tasks.md Task 7 budget rule).

    Exhausted ids are collected across the whole repair phase so the
    operator sees every repair the orchestrator attempted before the
    halt fires, rather than halting on the first one and hiding the
    rest.
    """
    exhausted_ids: list[str] = []
    # Lazy-loaded set of branch names for tasks currently in `ready`
    # state. Populated on first `OrphanBranch` finding to avoid an
    # extra `bd` call when reconciliation surfaces no orphan branches.
    ready_branches: frozenset[str] | None = None

    for finding in report.findings:
        match finding:
            case MissingWorktree(task_id=task_id):
                if _handle_failure(
                    services,
                    task_id,
                    "reconcile: worktree missing; releasing claim",
                ):
                    exhausted_ids.append(task_id)
                services.emitter.emit(
                    "repair",
                    action="release_claim_missing_worktree",
                    task_id=task_id,
                )

            case CompletionPending(task_id=task_id):
                pr_url = _complete_pending_task(feature, task_id, services)
                services.emitter.emit(
                    "repair",
                    action="completion_pending",
                    task_id=task_id,
                    pr_url=pr_url,
                )

            case CompletionPendingWithPr(task_id=task_id, pr_url=pr_url):
                # PR already open; record its number on the bd task
                # via `mark_pr_open` and leave the task in_progress.
                # The merge-advancement sweep on a future
                # `turma run` will close + cleanup once the PR
                # merges. Mirrors the defer-close shape
                # `_run_single_task` adopted in Task 3.
                pr_number = _pr_number_from_url(pr_url)
                services.beads.mark_pr_open(task_id, pr_number)
                _revert_beads_export(services)
                services.emitter.emit(
                    "repair",
                    action="completion_pending_with_pr",
                    task_id=task_id,
                    pr_url=pr_url,
                )

            case FailurePending(task_id=task_id, reason=reason):
                if _handle_failure(
                    services, task_id, f"reconcile: {reason}"
                ):
                    exhausted_ids.append(task_id)
                services.emitter.emit(
                    "repair", action="fail_task", task_id=task_id, reason=reason
                )

            case StaleNoSentinels(task_id=task_id):
                raise PlanningError(
                    f"stale worktree for {task_id} has no sentinels; "
                    f"operator decides. Inspect "
                    f"`bd show {task_id}` and "
                    f"`.worktrees/{feature}/{task_id}/` before "
                    "re-running."
                )

            case OrphanBranch(branch=branch):
                # Reconciliation's v1 contract defines orphan-branch
                # as "no corresponding in_progress task"; a branch
                # belonging to a `ready` task (i.e. a failed-not-
                # exhausted retry about to be re-claimed by the main
                # loop in this same run) still matches that
                # definition, but the operator-facing "orphan branch
                # (operator triage)" log line reads as misleading
                # because the branch is not actually abandoned.
                # Suppress the log for that retry case; the
                # reconciliation summary's `→ orphan-branch` line
                # printed upstream still appears, so telemetry /
                # reports see the classification.
                if ready_branches is None:
                    ready_branches = frozenset(
                        services.worktree.branch_name_for(feature, t.id)
                        for t in services.beads.list_ready_tasks(feature)
                    )
                if branch in ready_branches:
                    continue
                services.emitter.emit("repair_orphan_branch", branch=branch)

    if exhausted_ids:
        joined = ", ".join(exhausted_ids)
        raise SwarmHalted(
            f"retry budget exhausted on {joined} during repair phase; "
            "halting run. Triage with `bd list --label "
            "needs_human_review`."
        )


# ---------------------------------------------------------------------
# Merge-advancement phase
# ---------------------------------------------------------------------


def _advance_merged_prs(
    feature: str,
    services: SwarmServices,
    *,
    dry_run: bool,
) -> None:
    """Sweep in_progress tasks bearing a `turma-pr:<N>` label and
    advance each per the PR's current GitHub state.

    Per
    `openspec/changes/swarm-post-merge-advancement/design.md`:

    - `state == "MERGED"` → `unmark_pr_open` → `close_task` →
      `cleanup_worktree`. The deferred close + cleanup that
      `_run_single_task` no longer fires lands here.
    - `state == "OPEN"` → leave alone. Draft PRs return
      `state == "OPEN"` from `--json state` (`isDraft` is not
      queried in v1) and fall through this branch unchanged.
    - `state == "CLOSED"` (no merge) → `unmark_pr_open` →
      `_handle_failure` with the canned reason
      `PR #<N> closed without merge`. Full retry-budget
      machinery applies; an exhausted-budget result is
      collected and raised after the per-task loop, matching
      the repair phase's existing pattern.
    - 404 from `gh` (recorded number does not exist) → halt
      the run with a typed `PlanningError` naming the task
      and pointing the operator at `bd show` for triage.

    On `dry_run=True` the sweep performs the PR-state reads
    but **no mutations** — it logs `would: <line>` for each
    task it would otherwise advance, so the operator gets a
    preview of what the next non-dry-run invocation will do.
    """
    in_progress = services.beads.list_in_progress_tasks(feature)
    exhausted_ids: list[str] = []

    for task in in_progress:
        pr_number = _extract_pr_number(task.labels)
        if pr_number is None:
            # No `turma-pr:<N>` label. Reconciliation already
            # owns the "in_progress without label" cases via
            # `completion-pending` / `stale-no-sentinels`.
            continue

        try:
            pr_state = services.pr.get_pr_state_by_number(pr_number)
        except PlanningError as exc:
            if "not found via gh" in str(exc):
                services.emitter.emit(
                    "merge_advancement",
                    action="halting_stale",
                    task_id=task.id,
                    pr_number=pr_number,
                    pr_state="not_found",
                    dry_run=dry_run,
                )
                raise PlanningError(
                    f"merge-advancement: PR #{pr_number} for task "
                    f"{task.id} not found via gh; the "
                    f"`turma-pr:{pr_number}` label is stale. "
                    f"Triage with `bd show {task.id}` and "
                    "`gh pr list --search 'head:task/<feature>/'`."
                ) from exc
            raise

        if pr_state.state == "MERGED":
            services.emitter.emit(
                "merge_advancement",
                action="closed",
                task_id=task.id,
                pr_number=pr_number,
                pr_state="MERGED",
                dry_run=dry_run,
            )
            if not dry_run:
                services.beads.unmark_pr_open(task.id, pr_number)
                services.beads.close_task(task.id)
                ref = _ref_for(feature, task.id, services)
                services.worktree.cleanup(ref)
                # Both unmark + close fired bd's hook; one revert
                # at end of arm clears the export-file dirtiness.
                _revert_beads_export(services)

        elif pr_state.state == "CLOSED":
            services.emitter.emit(
                "merge_advancement",
                action="failed",
                task_id=task.id,
                pr_number=pr_number,
                pr_state="CLOSED",
                dry_run=dry_run,
            )
            if not dry_run:
                services.beads.unmark_pr_open(task.id, pr_number)
                if _handle_failure(
                    services,
                    task.id,
                    f"PR #{pr_number} closed without merge",
                ):
                    exhausted_ids.append(task.id)

        elif pr_state.state == "OPEN":
            # Draft PRs surface as OPEN here; v1 does not
            # differentiate.
            services.emitter.emit(
                "merge_advancement",
                action="left_alone",
                task_id=task.id,
                pr_number=pr_number,
                pr_state="OPEN",
                dry_run=dry_run,
            )

        else:
            # Unknown state — log and leave alone. If `gh` ever
            # adds a new state value, surfacing it here keeps
            # the orchestrator honest without a hard halt.
            services.emitter.emit(
                "merge_advancement",
                action="left_alone_unrecognized",
                task_id=task.id,
                pr_number=pr_number,
                pr_state=pr_state.state,
                dry_run=dry_run,
            )

    if exhausted_ids:
        joined = ", ".join(exhausted_ids)
        raise SwarmHalted(
            f"retry budget exhausted on {joined} during "
            "merge-advancement phase; halting run. Triage with "
            "`bd list --label needs_human_review`."
        )


# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------


def _main_loop(
    feature: str,
    services: SwarmServices,
    max_tasks: int | None,
) -> None:
    """fetch_ready → claim → worktree → worker → commit/push/PR/close.

    Exits cleanly when no ready tasks remain. Halts with a
    PlanningError if a task exhausts its retry budget, matching the
    `END_fail` terminal in the state machine diagram.
    """
    iterations = 0
    while True:
        if max_tasks is not None and iterations >= max_tasks:
            services.emitter.emit("stopping_max_tasks", max_tasks=max_tasks)
            return

        ready = services.beads.list_ready_tasks(feature)
        if not ready:
            services.emitter.emit("done", reason="no_ready_tasks")
            return

        task = ready[0]

        try:
            services.beads.claim_task(task.id)
        except PlanningError as exc:
            # Claim race — another actor beat us. Skip this task and
            # re-fetch on the next iteration. Races do NOT consume
            # `max_tasks` budget: the operator asked for N tasks
            # end-to-end, not N claim attempts.
            services.emitter.emit(
                "claim_race", task_id=task.id, detail=str(exc)
            )
            continue

        # bd's hook dirtied .beads/issues.jsonl on claim_task; revert
        # to keep main's working tree clean across iterations.
        _revert_beads_export(services)

        iterations += 1
        services.emitter.emit(
            "task_claimed", task_id=task.id, title=task.title
        )

        exhausted = _run_single_task(feature, task, services)
        if exhausted:
            raise SwarmHalted(
                f"retry budget exhausted on {task.id}; halting run. "
                f"Triage with `bd show {task.id}` and "
                "`bd list --label needs_human_review`."
            )


def _run_single_task(
    feature: str,
    task: BeadsTaskRef,
    services: SwarmServices,
) -> bool:
    """Drive one claimed task through the state machine.

    Returns True iff the run halted on exhausted retry budget and the
    outer loop must stop.
    """
    ref = services.worktree.setup(
        feature=feature,
        task_id=task.id,
        base_branch=services.base_branch,
    )
    services.emitter.emit("worktree_setup", task_id=task.id)
    _clear_sentinels(ref.path)
    description = services.beads.get_task_body(task.id)
    invocation = WorkerInvocation(
        task_id=task.id,
        title=task.title,
        description=description,
        worktree=ref.path,
        timeout_seconds=services.worker_timeout,
    )
    worker = services.worker_factory()
    # Announce the long wait before blocking on worker.run — this is
    # the run's longest silent stretch (up to worker_timeout).
    services.emitter.emit(
        "worker_running", task_id=task.id, timeout_s=services.worker_timeout
    )
    result = worker.run(invocation)

    if result.status != "success":
        reason = result.reason or f"worker {result.status}"
        return _handle_failure(services, task.id, reason)

    if not services.git.status_is_dirty(ref.path, ignore_bd_export=True):
        return _handle_failure(services, task.id, CLEAN_TREE_REASON)

    try:
        message = _render_commit_message(task, feature)
        services.git.commit_worker_changes(ref.path, message)
        services.emitter.emit("commit", task_id=task.id)
        services.git.push_branch(ref.path, ref.branch)
        services.emitter.emit("push", task_id=task.id)
    except PlanningError as exc:
        return _handle_failure(services, task.id, str(exc))

    try:
        pr_url = services.pr.open_pr(
            branch=ref.branch,
            base=services.base_branch,
            title=_render_pr_title(task),
            body=_render_pr_body(task, description),
        )
    except PlanningError as exc:
        return _handle_failure(services, task.id, str(exc))

    # Defer `close_task` + `cleanup_worktree` to the merge-
    # advancement sweep on a future `turma run` invocation: a PR
    # has been opened, but the human reviewer hasn't merged it
    # yet. Until then the task stays `in_progress` with a
    # `turma-pr:<N>` label, the worktree stays on disk, and any
    # task that depends on this one stays blocked-by-deps. See
    # `openspec/changes/swarm-post-merge-advancement/` for the
    # full contract; the merge-advancement phase
    # (`_advance_merged_prs`, future task) consumes the label.
    pr_number = _pr_number_from_url(pr_url)
    services.beads.mark_pr_open(task.id, pr_number)
    _revert_beads_export(services)
    services.emitter.emit(
        "task_opened", task_id=task.id, pr_number=pr_number, pr_url=pr_url
    )
    return False


# ---------------------------------------------------------------------
# Worker-run hygiene
# ---------------------------------------------------------------------


def _clear_sentinels(worktree: Path) -> None:
    """Remove worker sentinels from `worktree` before invoking a worker.

    The orchestrator reuses a kept worktree on retry after a
    failed-not-exhausted attempt (failed worktrees stay on disk as
    the primary triage artifact — see the design's Worktree
    contract). A retry run whose worker exits without overwriting
    its prior attempt's sentinel would otherwise re-read the stale
    one via `worker._detect_sentinel_result`, contaminating the
    retry's reported outcome.

    Clearing here keeps the invariant "sentinels are fresh per
    attempt." The stale content has already been captured into
    Beads via `_handle_failure` on the failing attempt, so the
    unlink is lossless — the diff + logs inside the worktree
    remain as the primary triage artifact.

    Swallows only `FileNotFoundError` (the expected "already
    absent" case on fresh worktrees). `PermissionError` and other
    `OSError` subclasses propagate so filesystem breakage surfaces
    instead of being silently masked.
    """
    for sentinel in (TASK_COMPLETE_SENTINEL, TASK_FAILED_SENTINEL):
        try:
            (worktree / sentinel).unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------
# Failure + budget helpers
# ---------------------------------------------------------------------


def _handle_failure(
    services: SwarmServices,
    task_id: str,
    reason: str,
) -> bool:
    """Record a task failure and return True iff the budget is exhausted."""
    retries = services.beads.retries_so_far(task_id)
    services.beads.fail_task(
        task_id,
        reason,
        retries_so_far=retries,
        max_retries=services.max_retries,
    )
    # fail_task fires bd's hook → main's tree dirty. Revert so
    # the next iteration's fetch_and_ff_base stays clean.
    _revert_beads_export(services)
    exhausted = (retries + 1) > services.max_retries
    services.emitter.emit(
        "task_failed",
        task_id=task_id,
        attempt=retries + 1,
        max_attempts=services.max_retries + 1,
        reason=reason,
        budget_exhausted=exhausted,
    )
    return exhausted


# ---------------------------------------------------------------------
# Reconcile-repair helpers
# ---------------------------------------------------------------------


def _complete_pending_task(
    feature: str,
    task_id: str,
    services: SwarmServices,
) -> str:
    """Run the commit/push/open-pr tail for a reconciliation-
    detected `completion-pending` task and label the task with
    its new PR number.

    The `close_task` + `cleanup_worktree` finish moves to the
    merge-advancement sweep on a future `turma run` invocation —
    the same defer-close shape `_run_single_task` adopted in
    Task 3 of `swarm-post-merge-advancement`. Until the PR is
    merged on GitHub, the task stays `in_progress` with a
    `turma-pr:<N>` label and its worktree on disk.
    """
    ref = _ref_for(feature, task_id, services)
    task = _lookup_task(services.beads, feature, task_id)
    description = services.beads.get_task_body(task_id)
    message = _render_commit_message(task, feature)
    services.git.commit_worker_changes(ref.path, message)
    services.git.push_branch(ref.path, ref.branch)
    pr_url = services.pr.open_pr(
        branch=ref.branch,
        base=services.base_branch,
        title=_render_pr_title(task),
        body=_render_pr_body(task, description),
    )
    pr_number = _pr_number_from_url(pr_url)
    services.beads.mark_pr_open(task_id, pr_number)
    _revert_beads_export(services)
    return pr_url


def _ref_for(
    feature: str, task_id: str, services: SwarmServices
) -> WorktreeRef:
    return WorktreeRef(
        path=services.worktree.worktree_path_for(feature, task_id),
        branch=services.worktree.branch_name_for(feature, task_id),
    )


def _lookup_task(
    beads: BeadsAdapter, feature: str, task_id: str
) -> BeadsTaskRef:
    """Re-hydrate a BeadsTaskRef for a repair-phase task.

    Reconciliation observed the task by id; we need its title/labels
    for the commit/PR templates. Falls back to a minimal ref if the
    task no longer appears in feature listings (shouldn't happen, but
    avoids a crash during repair).
    """
    for ref in beads.list_in_progress_tasks(feature):
        if ref.id == task_id:
            return ref
    return BeadsTaskRef(id=task_id, title=task_id, labels=())


# ---------------------------------------------------------------------
# Commit / PR templates
# ---------------------------------------------------------------------


def _turma_type_of(task: BeadsTaskRef) -> str:
    for label in task.labels:
        if label.startswith(TURMA_TYPE_LABEL_PREFIX):
            return label[len(TURMA_TYPE_LABEL_PREFIX):] or _DEFAULT_TURMA_TYPE
    return _DEFAULT_TURMA_TYPE


def _render_commit_message(task: BeadsTaskRef, feature: str) -> str:
    return COMMIT_MESSAGE_TEMPLATE.format(
        turma_type=_turma_type_of(task),
        task_title=task.title,
        task_id=task.id,
        feature=feature,
    )


def _render_pr_title(task: BeadsTaskRef) -> str:
    return f"[{_turma_type_of(task)}] {task.title}"


def _render_pr_body(task: BeadsTaskRef, description: str) -> str:
    tail = description.strip()
    if tail:
        return f"Closes {task.id}.\n\n{tail}"
    return f"Closes {task.id}."
