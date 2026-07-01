"""Read-only feature status readout for `turma status --feature <name>`.

Composes Beads + worktree + GitHub PR state into a compact text
block the operator can read at a glance. Pure function — all state
comes through the `SwarmServices` parameter, return value is the
rendered text.

**No-mutation invariant.** This module never calls any mutating
adapter surface (`claim_task`, `close_task`, `fail_task`, `setup`,
`cleanup`, `commit_all`, `push_branch`, `open_pr`). The headline
test in `tests/test_swarm_status.py` asserts zero calls to each.

Output shape is pinned by
`openspec/changes/turma-status/design.md` — see the "Output
sections" block there for the reference rendering. The orphan
branches section uses `reconcile_feature`'s exact
`in_progress`-only filter and does not redefine the v1
reconciliation contract (see `design.md` "orphan branches"
subsection for the rationale).

The adapter read pipeline was originally pinned as five steps
(bd snapshots → ready → in-progress + retries → branches →
list_prs_for_feature). The merge-advancement arc adds an
additive sixth phase: per-in-progress-task
`get_pr_state_by_number` gated on the `turma-pr:<N>` label.
See `openspec/changes/swarm-post-merge-advancement/design.md`
"`turma status` read-pipeline addition" for the contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from turma.swarm._orchestrator import SwarmServices
from turma.swarm.pull_request import PrState, PrSummary
from turma.swarm.worker import TASK_COMPLETE_SENTINEL, TASK_FAILED_SENTINEL
from turma.transcription.beads import (
    BeadsTaskRef,
    BeadsTaskSnapshot,
    _extract_pr_number,
)


NEEDS_HUMAN_REVIEW_LABEL = "needs_human_review"

# Stable identifier for the --json payload contract
# (openspec/changes/turma-status-json). Not a versioning system —
# bumping it later is a future decision.
STATUS_SCHEMA = "turma.status.v1"

_PLAN_HINT = "run `turma plan --feature {name}` first"
_PLAN_TO_BEADS_HINT = "run `turma plan-to-beads --feature {name}` next"


# ---------------------------------------------------------------------
# Structured intermediates (kept module-private)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Preflight:
    change_dir: Path
    change_dir_exists: bool
    approved: bool
    transcribed: bool


@dataclass(frozen=True)
class _Buckets:
    ready: int
    in_progress: int
    blocked_deferred: int
    closed: int
    needs_human_review: int


class _WorktreeView(Protocol):
    def worktree_path_for(self, feature: str, task_id: str) -> Path: ...
    def branch_name_for(self, feature: str, task_id: str) -> str: ...
    def list_task_branches(self, feature: str) -> tuple[str, ...]: ...


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------


def status_readout(
    feature: str,
    *,
    services: SwarmServices,
    repo_root: Path,
    as_json: bool = False,
) -> str:
    """Compose a read-only status readout for `feature` into a string.

    Raises `PlanningError` from any adapter call that fails — a
    partial readout is worse than an explicit error. Missing spec
    dir / APPROVED / TRANSCRIBED.md markers render inline as `no`
    (the command's job is to show state, including "no state yet").

    `as_json=True` selects the machine-readable renderer
    (`turma.status.v1`) over the text one. Both consume the same
    gathered state, so the read-only/no-mutation invariant is
    identical; only the final formatting differs.
    """
    preflight = _check_preflight(feature, repo_root)

    # Adapter reads in the order pinned by the turma-status arc
    # plus the additive sixth phase from the merge-advancement
    # arc (see module docstring for spec references):
    #   1. all-statuses snapshots (for counters + orphan filter).
    #   2. ready task list.
    #   3. in-progress task list + per-task retries.
    #   4. worktree branches.
    #   5. PRs for the feature.
    #   6. per-in-progress-task PR state, gated on the
    #      `turma-pr:<N>` label (additive — labelless tasks
    #      trigger zero gh I/O).
    all_snapshots = services.beads.list_feature_tasks_all_statuses(feature)
    ready_tasks = services.beads.list_ready_tasks(feature)
    in_progress_tasks = services.beads.list_in_progress_tasks(feature)

    ready_ids = {t.id for t in ready_tasks}
    buckets = _bucket_tasks(all_snapshots, ready_ids)

    in_progress_retries = {
        task.id: services.beads.retries_so_far(task.id)
        for task in in_progress_tasks
    }

    branches = services.worktree.list_task_branches(feature)
    prs = services.pr.list_prs_for_feature(feature, services.worktree)

    in_progress_pr_states: dict[str, PrState] = {}
    for task in in_progress_tasks:
        pr_number = _extract_pr_number(task.labels)
        if pr_number is None:
            continue
        in_progress_pr_states[task.id] = (
            services.pr.get_pr_state_by_number(pr_number)
        )

    render_kwargs = dict(
        feature=feature,
        preflight=preflight,
        buckets=buckets,
        ready_tasks=ready_tasks,
        in_progress_tasks=in_progress_tasks,
        in_progress_retries=in_progress_retries,
        in_progress_pr_states=in_progress_pr_states,
        max_retries=services.max_retries,
        worktree_manager=services.worktree,
        branches=branches,
        prs=prs,
    )
    return _render_json(**render_kwargs) if as_json else _render(**render_kwargs)


# ---------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------


def _check_preflight(feature: str, repo_root: Path) -> _Preflight:
    change_dir = repo_root / "openspec" / "changes" / feature
    if not change_dir.is_dir():
        return _Preflight(
            change_dir=change_dir,
            change_dir_exists=False,
            approved=False,
            transcribed=False,
        )
    return _Preflight(
        change_dir=change_dir,
        change_dir_exists=True,
        approved=(change_dir / "APPROVED").exists(),
        transcribed=(change_dir / "TRANSCRIBED.md").exists(),
    )


# ---------------------------------------------------------------------
# Bucket counting
# ---------------------------------------------------------------------


def _bucket_tasks(
    snapshots: Iterable[BeadsTaskSnapshot],
    ready_ids: set[str],
) -> _Buckets:
    """Partition all-statuses snapshots into mutually exclusive
    counter buckets. Priority order:

    1. `needs_human_review` label (regardless of bd status).
    2. bd status == `in_progress`.
    3. bd status == `closed`.
    4. bd status in (`blocked`, `deferred`).
    5. bd status == `open` and id is in the ready set → `ready`.
    6. bd status == `open` and id is NOT in the ready set → also
       `blocked_deferred` (dependency-blocked — bd's `ready` view
       filtered it out).

    Tasks with an unrecognized status are silently dropped
    (forward-compat with any bd status vocabulary evolution).
    """
    ready = in_progress = blocked_deferred = closed = needs_human_review = 0
    for snap in snapshots:
        if NEEDS_HUMAN_REVIEW_LABEL in snap.labels:
            needs_human_review += 1
            continue
        if snap.status == "in_progress":
            in_progress += 1
        elif snap.status == "closed":
            closed += 1
        elif snap.status in ("blocked", "deferred"):
            blocked_deferred += 1
        elif snap.status == "open":
            if snap.id in ready_ids:
                ready += 1
            else:
                blocked_deferred += 1
    return _Buckets(
        ready=ready,
        in_progress=in_progress,
        blocked_deferred=blocked_deferred,
        closed=closed,
        needs_human_review=needs_human_review,
    )


# ---------------------------------------------------------------------
# Sentinel inspection (lossless — read-only)
# ---------------------------------------------------------------------


def _describe_sentinel(worktree: Path) -> str:
    """Return the per-task sentinel description for the in-progress
    section. `.task_complete` wins over `.task_failed` (matches the
    worker module's precedence). Failure reason is truncated to the
    first line so a multi-line `.task_failed` body doesn't blow up
    the readout — the full file is still on disk for triage."""
    complete = worktree / TASK_COMPLETE_SENTINEL
    failed = worktree / TASK_FAILED_SENTINEL
    if complete.exists():
        return "complete"
    if failed.exists():
        try:
            text = failed.read_text()
        except OSError:
            return 'failed: "<could not read sentinel>"'
        first_line = text.splitlines()[0] if text.splitlines() else ""
        return f'failed: "{first_line.strip()}"'
    return "none"


def _sentinel_struct(worktree: Path) -> dict[str, object]:
    """Structured form of the sentinel for the JSON renderer, mirroring
    `_describe_sentinel`'s precedence, first-line truncation, and
    unreadable-file fallback as `{status, reason}`."""
    complete = worktree / TASK_COMPLETE_SENTINEL
    failed = worktree / TASK_FAILED_SENTINEL
    if complete.exists():
        return {"status": "complete", "reason": None}
    if failed.exists():
        try:
            text = failed.read_text()
        except OSError:
            return {"status": "failed", "reason": "<could not read sentinel>"}
        lines = text.splitlines()
        return {"status": "failed", "reason": lines[0].strip() if lines else ""}
    return {"status": None, "reason": None}


# ---------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------


def _render(
    *,
    feature: str,
    preflight: _Preflight,
    buckets: _Buckets,
    ready_tasks: tuple[BeadsTaskRef, ...],
    in_progress_tasks: tuple[BeadsTaskRef, ...],
    in_progress_retries: dict[str, int],
    in_progress_pr_states: dict[str, PrState],
    max_retries: int,
    worktree_manager: _WorktreeView,
    branches: tuple[str, ...],
    prs: tuple[PrSummary, ...],
) -> str:
    sections = [
        _render_feature_header(feature, preflight),
        _render_counter_block(buckets),
        _render_ready(ready_tasks),
        _render_in_progress(
            feature=feature,
            in_progress_tasks=in_progress_tasks,
            retries=in_progress_retries,
            pr_states=in_progress_pr_states,
            max_retries=max_retries,
            worktree_manager=worktree_manager,
        ),
        _render_prs(prs),
        _render_orphan_branches(feature, branches, in_progress_tasks, worktree_manager),
    ]
    return "\n\n".join(sections) + "\n"


def _render_feature_header(feature: str, pre: _Preflight) -> str:
    lines = [f"feature: {feature}"]
    if pre.change_dir_exists:
        spec_path = f"openspec/changes/{feature}/"
    else:
        spec_path = (
            f"openspec/changes/{feature}/ (not present; "
            + _PLAN_HINT.format(name=feature)
            + ")"
        )
    lines.append(f"  spec: {spec_path}")
    lines.append(f"  approved: {'yes' if pre.approved else 'no'}")
    transcribed_line = f"  transcribed: {'yes' if pre.transcribed else 'no'}"
    if (
        pre.change_dir_exists
        and pre.approved
        and not pre.transcribed
    ):
        transcribed_line += (
            " (" + _PLAN_TO_BEADS_HINT.format(name=feature) + ")"
        )
    lines.append(transcribed_line)
    return "\n".join(lines)


def _render_counter_block(b: _Buckets) -> str:
    return "\n".join([
        "tasks:",
        f"  ready:              {b.ready}",
        f"  in_progress:        {b.in_progress}",
        f"  blocked / deferred: {b.blocked_deferred}",
        f"  closed:             {b.closed}",
        f"  needs_human_review: {b.needs_human_review}",
    ])


def _render_ready(tasks: tuple[BeadsTaskRef, ...]) -> str:
    lines = ["ready tasks:"]
    if not tasks:
        lines.append("  (none)")
    else:
        for t in tasks:
            lines.append(f"  {t.id} — {t.title}")
    return "\n".join(lines)


def _render_in_progress(
    *,
    feature: str,
    in_progress_tasks: tuple[BeadsTaskRef, ...],
    retries: dict[str, int],
    pr_states: dict[str, PrState],
    max_retries: int,
    worktree_manager: _WorktreeView,
) -> str:
    lines = ["in-progress tasks:"]
    if not in_progress_tasks:
        lines.append("  (none)")
        return "\n".join(lines)
    for task in in_progress_tasks:
        lines.append(f"  {task.id} — {task.title}")
        attempts = retries.get(task.id, 0)
        lines.append(f"    retries: {attempts} / {max_retries}")
        worktree = worktree_manager.worktree_path_for(feature, task.id)
        if worktree.is_dir():
            lines.append(f"    worktree: {worktree}/ (present)")
            lines.append(f"    sentinel: {_describe_sentinel(worktree)}")
        else:
            lines.append(f"    worktree: {worktree}/ (absent)")
            lines.append("    sentinel: none")
        pr_state = pr_states.get(task.id)
        if pr_state is not None:
            lines.append(
                f"    pr: #{pr_state.number} ({pr_state.state}) {pr_state.url}"
            )
    return "\n".join(lines)


def _render_prs(prs: tuple[PrSummary, ...]) -> str:
    lines = ["pull requests:"]
    if not prs:
        lines.append("  (none)")
        return "\n".join(lines)
    for pr in prs:
        lines.append(f"  #{pr.number} {pr.state} — {pr.title}")
        lines.append(f"    head: {pr.head_branch}")
        lines.append(f"    url:  {pr.url}")
    return "\n".join(lines)


def _orphan_branch_names(
    feature: str,
    branches: tuple[str, ...],
    in_progress_tasks: tuple[BeadsTaskRef, ...],
    worktree_manager: _WorktreeView,
) -> list[str]:
    """Feature branches with no in_progress task — the same filter the
    text and JSON renderers both surface. Mirrors reconciliation's
    `in_progress`-only orphan definition."""
    in_progress_branches = {
        worktree_manager.branch_name_for(feature, t.id)
        for t in in_progress_tasks
    }
    return [b for b in branches if b not in in_progress_branches]


def _render_orphan_branches(
    feature: str,
    branches: tuple[str, ...],
    in_progress_tasks: tuple[BeadsTaskRef, ...],
    worktree_manager: _WorktreeView,
) -> str:
    lines = ["orphan branches:"]
    orphans = _orphan_branch_names(
        feature, branches, in_progress_tasks, worktree_manager
    )
    if not orphans:
        lines.append("  (none)")
    else:
        for branch in orphans:
            lines.append(f"  {branch}  (no in_progress task)")
    return "\n".join(lines)


# ---------------------------------------------------------------------
# JSON rendering (turma.status.v1)
# ---------------------------------------------------------------------


def _render_json(
    *,
    feature: str,
    preflight: _Preflight,
    buckets: _Buckets,
    ready_tasks: tuple[BeadsTaskRef, ...],
    in_progress_tasks: tuple[BeadsTaskRef, ...],
    in_progress_retries: dict[str, int],
    in_progress_pr_states: dict[str, PrState],
    max_retries: int,
    worktree_manager: _WorktreeView,
    branches: tuple[str, ...],
    prs: tuple[PrSummary, ...],
) -> str:
    """Render the gathered status model as `turma.status.v1` JSON.

    Pure formatting over the same data the text renderer consumes;
    mirrors the text sections 1:1 in gathered order. `indent=2`, no
    `sort_keys` (insertion order is the contract)."""
    payload = {
        "schema": STATUS_SCHEMA,
        "feature": feature,
        "spec": {
            "change_dir": f"openspec/changes/{feature}/",
            "present": preflight.change_dir_exists,
            "approved": preflight.approved,
            "transcribed": preflight.transcribed,
        },
        "tasks": {
            "ready": buckets.ready,
            "in_progress": buckets.in_progress,
            "blocked_deferred": buckets.blocked_deferred,
            "closed": buckets.closed,
            "needs_human_review": buckets.needs_human_review,
        },
        "ready": [{"id": t.id, "title": t.title} for t in ready_tasks],
        "in_progress": [
            _in_progress_json_entry(
                feature,
                task,
                in_progress_retries,
                in_progress_pr_states,
                max_retries,
                worktree_manager,
            )
            for task in in_progress_tasks
        ],
        "pull_requests": [
            {
                "number": pr.number,
                "url": pr.url,
                "state": pr.state,
                "title": pr.title,
                "head_branch": pr.head_branch,
            }
            for pr in prs
        ],
        "orphan_branches": _orphan_branch_names(
            feature, branches, in_progress_tasks, worktree_manager
        ),
    }
    return json.dumps(payload, indent=2)


def _in_progress_json_entry(
    feature: str,
    task: BeadsTaskRef,
    retries: dict[str, int],
    pr_states: dict[str, PrState],
    max_retries: int,
    worktree_manager: _WorktreeView,
) -> dict[str, object]:
    worktree = worktree_manager.worktree_path_for(feature, task.id)
    present = worktree.is_dir()
    sentinel = (
        _sentinel_struct(worktree)
        if present
        else {"status": None, "reason": None}
    )
    pr_state = pr_states.get(task.id)
    pr = (
        None
        if pr_state is None
        else {
            "number": pr_state.number,
            "state": pr_state.state,
            "url": pr_state.url,
        }
    )
    return {
        "id": task.id,
        "title": task.title,
        "retries": retries.get(task.id, 0),
        "max_retries": max_retries,
        "worktree": {
            "present": present,
            "path": str(worktree) if present else None,
        },
        "sentinel": sentinel,
        "pr": pr,
    }
