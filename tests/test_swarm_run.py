"""Integration tests for the swarm orchestrator (`run_swarm`).

Every adapter is stubbed; we assert on the ordered sequence of
adapter calls rather than subprocess argv. Each scenario from the
Task 7 checklist gets at least one test:

- one-task happy loop
- multi-task sequential loop
- claim race (other run won the task)
- worker success + clean tree → retry path
- worker failure + budget remaining → retry path
- worker failure + budget exhausted → halts outer loop
- each reconciliation repair finding drives the expected adapter calls
- --dry-run never calls any mutation
- preflight failures
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from turma.errors import PlanningError
from turma.swarm import SwarmServices, run_swarm
from turma.swarm._orchestrator import _pr_number_from_url
from turma.swarm.pull_request import PrState
from turma.swarm.worker import WorkerInvocation, WorkerResult
from turma.swarm.worktree import WorktreeRef
from turma.transcription.beads import BeadsTaskRef


# ---------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------


@dataclass
class StubBeads:
    ready_queue: list[tuple[BeadsTaskRef, ...]] = field(default_factory=list)
    in_progress: tuple[BeadsTaskRef, ...] = ()
    bodies: dict[str, str] = field(default_factory=dict)
    retries: dict[str, int] = field(default_factory=dict)
    claim_raises_for: set[str] = field(default_factory=set)
    calls: list[tuple] = field(default_factory=list)
    failed: list[tuple[str, str, int, int]] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    pr_marked: list[tuple[str, int]] = field(default_factory=list)
    pr_unmarked: list[tuple[str, int]] = field(default_factory=list)

    # read-only surfaces --------------------------------------------------

    def list_ready_tasks(self, feature: str) -> tuple[BeadsTaskRef, ...]:
        self.calls.append(("list_ready_tasks", feature))
        if not self.ready_queue:
            return ()
        head = self.ready_queue[0]
        return head

    def list_in_progress_tasks(
        self, feature: str
    ) -> tuple[BeadsTaskRef, ...]:
        self.calls.append(("list_in_progress_tasks", feature))
        return self.in_progress

    def get_task_body(self, task_id: str) -> str:
        self.calls.append(("get_task_body", task_id))
        return self.bodies.get(task_id, "")

    def retries_so_far(self, task_id: str) -> int:
        self.calls.append(("retries_so_far", task_id))
        return self.retries.get(task_id, 0)

    # mutation surfaces ---------------------------------------------------

    def claim_task(self, task_id: str) -> None:
        self.calls.append(("claim_task", task_id))
        if task_id in self.claim_raises_for:
            # Simulate real `bd ready` semantics: once another actor
            # has claimed the task, it is no longer in `open` state so
            # subsequent `bd ready` calls won't include it. Drop it
            # from the current queue head so the orchestrator's
            # re-fetch surfaces the next task.
            self._drop_from_current_queue_head(task_id)
            raise PlanningError(
                f"claim race: {task_id} already claimed by another actor"
            )
        # Normal successful claim: advance the queue. Next
        # `list_ready_tasks` call returns the next configured entry.
        self.ready_queue.pop(0)
        self.ready_queue.append(())

    def _drop_from_current_queue_head(self, task_id: str) -> None:
        if not self.ready_queue:
            return
        self.ready_queue[0] = tuple(
            t for t in self.ready_queue[0] if t.id != task_id
        )

    def close_task(self, task_id: str) -> None:
        self.calls.append(("close_task", task_id))
        self.closed.append(task_id)

    def mark_pr_open(self, task_id: str, pr_number: int) -> None:
        self.calls.append(("mark_pr_open", task_id, pr_number))
        self.pr_marked.append((task_id, pr_number))

    def unmark_pr_open(self, task_id: str, pr_number: int) -> None:
        self.calls.append(("unmark_pr_open", task_id, pr_number))
        self.pr_unmarked.append((task_id, pr_number))

    def fail_task(
        self,
        task_id: str,
        reason: str,
        *,
        retries_so_far: int,
        max_retries: int,
    ) -> None:
        self.calls.append(
            ("fail_task", task_id, reason, retries_so_far, max_retries)
        )
        self.failed.append((task_id, reason, retries_so_far, max_retries))
        # Bump retry count so the next retries_so_far read reflects
        # the post-fail state.
        self.retries[task_id] = retries_so_far + 1

    # claim-race variant: after the race, allow the next ready fetch
    # to skip the raced task.
    def skip_claimed(self, task_id: str) -> None:
        for i, queue in enumerate(self.ready_queue):
            self.ready_queue[i] = tuple(t for t in queue if t.id != task_id)


@dataclass
class StubWorktree:
    repo_root: Path
    calls: list[tuple] = field(default_factory=list)

    def worktree_path_for(self, feature: str, task_id: str) -> Path:
        self.calls.append(("worktree_path_for", feature, task_id))
        return self.repo_root / ".worktrees" / feature / task_id

    def branch_name_for(self, feature: str, task_id: str) -> str:
        self.calls.append(("branch_name_for", feature, task_id))
        return f"task/{feature}/{task_id}"

    def list_task_branches(self, feature: str) -> tuple[str, ...]:
        self.calls.append(("list_task_branches", feature))
        return ()

    def setup(
        self, *, feature: str, task_id: str, base_branch: str
    ) -> WorktreeRef:
        self.calls.append(("setup", feature, task_id, base_branch))
        path = self.repo_root / ".worktrees" / feature / task_id
        path.mkdir(parents=True, exist_ok=True)
        return WorktreeRef(path=path, branch=f"task/{feature}/{task_id}")

    def cleanup(self, ref: WorktreeRef) -> None:
        self.calls.append(("cleanup", str(ref.path), ref.branch))


@dataclass
class StubGit:
    dirty: bool = True
    commit_raises: PlanningError | None = None
    push_raises: PlanningError | None = None
    calls: list[tuple] = field(default_factory=list)

    def status_is_dirty(self, worktree: Path) -> bool:
        self.calls.append(("status_is_dirty", str(worktree)))
        return self.dirty

    def commit_all(self, worktree: Path, message: str) -> str:
        self.calls.append(("commit_all", str(worktree), message))
        if self.commit_raises is not None:
            raise self.commit_raises
        return "deadbeef"

    def push_branch(
        self, worktree: Path, branch: str, *, remote: str = "origin"
    ) -> None:
        self.calls.append(("push_branch", str(worktree), branch, remote))
        if self.push_raises is not None:
            raise self.push_raises


@dataclass
class StubPr:
    url: str = "https://github.com/example/repo/pull/1"
    open_raises: PlanningError | None = None
    find_raises: PlanningError | None = None
    # Per-PR-number response table for `get_pr_state_by_number`.
    # `None` value means raise `PlanningError("...not found via gh...")`
    # (the typed 404 path the merge-advancement sweep recognizes).
    pr_states: dict[int, PrState | None] = field(default_factory=dict)
    state_raises: PlanningError | None = None
    calls: list[tuple] = field(default_factory=list)

    def open_pr(
        self, *, branch: str, base: str, title: str, body: str
    ) -> str:
        self.calls.append(("open_pr", branch, base, title, body))
        if self.open_raises is not None:
            raise self.open_raises
        return self.url

    def find_open_pr_url_for_branch(self, branch: str) -> str | None:
        self.calls.append(("find_open_pr_url_for_branch", branch))
        if self.find_raises is not None:
            raise self.find_raises
        return None

    def get_pr_state_by_number(self, pr_number: int) -> PrState:
        self.calls.append(("get_pr_state_by_number", pr_number))
        if self.state_raises is not None:
            raise self.state_raises
        configured = self.pr_states.get(pr_number)
        if configured is None and pr_number in self.pr_states:
            # Explicit None → simulate the 404 typed error.
            raise PlanningError(
                f"PR #{pr_number} not found via gh; the "
                f"`turma-pr:{pr_number}` label is stale. Triage with "
                "`bd show <task_id>` and `gh pr list --search "
                "'head:task/<feature>/'`."
            )
        if configured is None:
            # Unconfigured number defaults to OPEN — convenient for
            # tests that don't care about the state.
            return PrState(
                number=pr_number,
                state="OPEN",
                url=f"https://example/pull/{pr_number}",
            )
        return configured


class StubWorker:
    def __init__(self, results: list[WorkerResult]) -> None:
        self.name = "stub"
        self._results = list(results)
        self.invocations: list[WorkerInvocation] = []

    def run(self, invocation: WorkerInvocation) -> WorkerResult:
        self.invocations.append(invocation)
        if not self._results:
            raise AssertionError(
                "StubWorker.run called more times than results provided"
            )
        return self._results.pop(0)


# ---------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------


def _scratch_feature(tmp_path: Path, feature: str = "oauth") -> Path:
    change_dir = tmp_path / "openspec" / "changes" / feature
    change_dir.mkdir(parents=True)
    (change_dir / "APPROVED").write_text("approved\n")
    (change_dir / "TRANSCRIBED.md").write_text("# transcribed\n")
    return change_dir


def _make_services(
    tmp_path: Path,
    *,
    beads: StubBeads,
    git: StubGit | None = None,
    pr: StubPr | None = None,
    worker_results: list[WorkerResult] | None = None,
    max_retries: int = 1,
) -> tuple[SwarmServices, StubWorktree, StubGit, StubPr, StubWorker]:
    wt = StubWorktree(repo_root=tmp_path)
    git = git or StubGit()
    pr = pr or StubPr()
    worker = StubWorker(results=worker_results or [])
    services = SwarmServices(
        beads=beads,  # type: ignore[arg-type]
        worktree=wt,  # type: ignore[arg-type]
        git=git,  # type: ignore[arg-type]
        pr=pr,  # type: ignore[arg-type]
        worker_factory=lambda: worker,  # type: ignore[return-value]
        repo_root=tmp_path,
        base_branch="main",
        max_retries=max_retries,
    )
    return services, wt, git, pr, worker


def _ref(task_id: str, title: str = "t", *, turma_type: str = "impl") -> BeadsTaskRef:
    return BeadsTaskRef(
        id=task_id,
        title=title,
        labels=("feature:oauth", f"turma-type:{turma_type}"),
    )


def _success() -> WorkerResult:
    return WorkerResult(status="success", reason="", stdout="", stderr="")


def _failure(reason: str) -> WorkerResult:
    return WorkerResult(
        status="failure", reason=reason, stdout="", stderr=""
    )


# ---------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------


def test_preflight_requires_services(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    with pytest.raises(PlanningError, match="requires a SwarmServices"):
        run_swarm("oauth", services=None)


def test_preflight_rejects_unknown_backend(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    services, *_ = _make_services(tmp_path, beads=StubBeads())
    with pytest.raises(PlanningError, match="unknown worker backend"):
        run_swarm("oauth", services=services, backend="codex")


def test_preflight_missing_change_dir(tmp_path: Path) -> None:
    services, *_ = _make_services(tmp_path, beads=StubBeads())
    with pytest.raises(PlanningError, match="no OpenSpec change directory"):
        run_swarm("oauth", services=services)


def test_preflight_missing_approved(tmp_path: Path) -> None:
    change_dir = tmp_path / "openspec" / "changes" / "oauth"
    change_dir.mkdir(parents=True)
    (change_dir / "TRANSCRIBED.md").write_text("x")
    services, *_ = _make_services(tmp_path, beads=StubBeads())
    with pytest.raises(PlanningError, match="not APPROVED"):
        run_swarm("oauth", services=services)


def test_preflight_missing_transcribed(tmp_path: Path) -> None:
    change_dir = tmp_path / "openspec" / "changes" / "oauth"
    change_dir.mkdir(parents=True)
    (change_dir / "APPROVED").write_text("x")
    services, *_ = _make_services(tmp_path, beads=StubBeads())
    with pytest.raises(PlanningError, match="not been transcribed"):
        run_swarm("oauth", services=services)


# ---------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------


def test_dry_run_never_calls_any_mutation(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    beads = StubBeads(ready_queue=[(_ref("bd-1"),)])
    services, wt, git, pr, worker = _make_services(
        tmp_path, beads=beads
    )
    run_swarm("oauth", services=services, dry_run=True)

    mutating_bd = {"claim_task", "close_task", "fail_task"}
    assert not any(c[0] in mutating_bd for c in beads.calls)
    mutating_wt = {"setup", "cleanup"}
    assert not any(c[0] in mutating_wt for c in wt.calls)
    mutating_git = {"commit_all", "push_branch"}
    assert not any(c[0] in mutating_git for c in git.calls)
    mutating_pr = {"open_pr"}
    assert not any(c[0] in mutating_pr for c in pr.calls)
    assert worker.invocations == []


# ---------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------


def test_single_task_happy_loop(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    task = _ref("bd-1", title="Wire OAuth")
    beads = StubBeads(
        ready_queue=[(task,)],
        bodies={"bd-1": "Subtask body here."},
    )
    services, wt, git, pr, worker = _make_services(
        tmp_path, beads=beads, worker_results=[_success()]
    )
    run_swarm("oauth", services=services)

    # Beads: claim, get_task_body (for invocation), mark_pr_open.
    # No fail. close_task is deferred to the merge-advancement
    # sweep on a future `turma run` invocation per
    # `openspec/changes/swarm-post-merge-advancement/`.
    ordered = [c[0] for c in beads.calls]
    assert "claim_task" in ordered
    assert "mark_pr_open" in ordered
    assert "close_task" not in ordered
    assert "fail_task" not in ordered
    # PR number 1 derives from the StubPr default URL
    # (`https://github.com/example/repo/pull/1`).
    assert beads.pr_marked == [("bd-1", 1)]

    # Worker received the rehydrated description.
    assert worker.invocations[0].description == "Subtask body here."
    assert worker.invocations[0].task_id == "bd-1"

    # Git path: dirty check → commit → push. No retry.
    git_steps = [c[0] for c in git.calls]
    assert git_steps == ["status_is_dirty", "commit_all", "push_branch"]

    # PR opened with the rendered template.
    assert len(pr.calls) == 1
    name, branch, base, title, body = pr.calls[0]
    assert name == "open_pr"
    assert branch == "task/oauth/bd-1"
    assert base == "main"
    assert title == "[impl] Wire OAuth"
    assert "Closes bd-1." in body
    assert "Subtask body here." in body

    # Worktree set up exactly once. Cleanup is deferred to the
    # merge-advancement sweep on a future invocation, not the
    # success path itself.
    assert [c[0] for c in wt.calls].count("setup") == 1
    assert [c[0] for c in wt.calls].count("cleanup") == 0


def test_multi_task_sequential_loop(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    t1, t2 = _ref("bd-1"), _ref("bd-2")
    # Two iterations: first round yields (t1,), second yields (t2,).
    beads = StubBeads(ready_queue=[(t1,), (t2,)])
    services, wt, git, pr, worker = _make_services(
        tmp_path, beads=beads, worker_results=[_success(), _success()]
    )
    run_swarm("oauth", services=services)

    # Both tasks reached the success-path tail and got labelled
    # `turma-pr:<N>`; close happens later via the merge-advancement
    # sweep on a future run.
    assert beads.pr_marked == [("bd-1", 1), ("bd-2", 1)]
    assert beads.closed == []
    # Two PR URLs opened; order matches claim order.
    assert [c[1] for c in pr.calls] == [
        "task/oauth/bd-1",
        "task/oauth/bd-2",
    ]


def test_max_tasks_caps_iterations(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    t1, t2 = _ref("bd-1"), _ref("bd-2")
    beads = StubBeads(ready_queue=[(t1,), (t2,)])
    services, *_ = _make_services(
        tmp_path, beads=beads, worker_results=[_success(), _success()]
    )
    run_swarm("oauth", services=services, max_tasks=1)

    # Only bd-1 was processed; loop stopped before bd-2. The
    # success path now ends at `mark_pr_open` rather than
    # `close_task` (close is deferred to the merge-advancement
    # sweep).
    assert beads.pr_marked == [("bd-1", 1)]
    assert beads.closed == []


# ---------------------------------------------------------------------
# Claim race
# ---------------------------------------------------------------------


def test_claim_race_skips_raced_task_and_continues(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    raced, winner = _ref("bd-race"), _ref("bd-winner")
    # Just one configured queue entry: the initial ready listing.
    # After the race, the stub drops bd-race from the head so the
    # orchestrator's re-fetch surfaces only bd-winner — matching real
    # `bd ready` semantics where a now-claimed-by-another task leaves
    # the list.
    beads = StubBeads(
        ready_queue=[(raced, winner)],
        claim_raises_for={"bd-race"},
    )
    services, *_, worker = _make_services(
        tmp_path, beads=beads, worker_results=[_success()]
    )
    run_swarm("oauth", services=services)

    # Raced task was attempted (claim raised), winner reached the
    # success-path tail (mark_pr_open). close_task is deferred to
    # the merge-advancement sweep.
    claim_ids = [c[1] for c in beads.calls if c[0] == "claim_task"]
    assert "bd-race" in claim_ids
    assert "bd-winner" in claim_ids
    assert beads.pr_marked == [("bd-winner", 1)]
    assert beads.closed == []
    # Worker only ran for the winner.
    assert [inv.task_id for inv in worker.invocations] == ["bd-winner"]


# ---------------------------------------------------------------------
# Clean-tree-after-success → retry
# ---------------------------------------------------------------------


def test_clean_tree_after_success_triggers_fail_task(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    task = _ref("bd-1")
    beads = StubBeads(ready_queue=[(task,), (task,)])
    services, wt, git, pr, worker = _make_services(
        tmp_path,
        beads=beads,
        git=StubGit(dirty=False),
        worker_results=[_success(), _success()],
        max_retries=1,
    )
    # First attempt: worker claims success but git shows clean tree
    # → fail_task. Second attempt: same path, budget exhausted → halt.
    with pytest.raises(PlanningError, match="budget exhausted"):
        run_swarm("oauth", services=services)

    # Two failures recorded, both with the clean-tree reason.
    reasons = [entry[1] for entry in beads.failed]
    assert reasons == [
        "worker reported success but left the tree clean",
        "worker reported success but left the tree clean",
    ]
    # No commit, no push, no PR.
    assert not any(c[0] == "commit_all" for c in git.calls)
    assert pr.calls == []


# ---------------------------------------------------------------------
# Worker failure paths
# ---------------------------------------------------------------------


def test_worker_failure_with_budget_remaining_retries(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    task = _ref("bd-1")
    beads = StubBeads(ready_queue=[(task,), (task,)])
    services, *_, worker = _make_services(
        tmp_path,
        beads=beads,
        worker_results=[_failure("timeout"), _success()],
        max_retries=1,
    )
    run_swarm("oauth", services=services)

    # One failure recorded; the retry reached the success-path
    # tail and labelled the task `turma-pr:<N>` (close happens
    # later via the merge-advancement sweep).
    assert [e[1] for e in beads.failed] == ["timeout"]
    assert beads.pr_marked == [("bd-1", 1)]
    assert beads.closed == []


def test_worker_failure_with_exhausted_budget_halts_loop(
    tmp_path: Path,
) -> None:
    _scratch_feature(tmp_path)
    task = _ref("bd-1")
    beads = StubBeads(
        ready_queue=[(task,)],
        retries={"bd-1": 1},  # already retried once; max_retries=1 → exhausted
    )
    services, *_ = _make_services(
        tmp_path,
        beads=beads,
        worker_results=[_failure("another failure")],
        max_retries=1,
    )
    with pytest.raises(PlanningError, match="budget exhausted"):
        run_swarm("oauth", services=services)

    # One fail_task recorded; no close.
    assert len(beads.failed) == 1
    assert beads.closed == []


def test_push_failure_enters_retry_path(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    task = _ref("bd-1")
    beads = StubBeads(ready_queue=[(task,)])
    services, *_ = _make_services(
        tmp_path,
        beads=beads,
        git=StubGit(push_raises=PlanningError("push: auth fail")),
        worker_results=[_success()],
        max_retries=0,  # zero retry budget → one attempt, then halt
    )
    with pytest.raises(PlanningError, match="budget exhausted"):
        run_swarm("oauth", services=services)

    assert [e[1] for e in beads.failed][0].startswith("push: auth fail") or (
        "push" in beads.failed[0][1]
    )


def test_open_pr_failure_enters_retry_path(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    task = _ref("bd-1")
    beads = StubBeads(ready_queue=[(task,)])
    services, *_ = _make_services(
        tmp_path,
        beads=beads,
        pr=StubPr(open_raises=PlanningError("gh: PAT blocked")),
        worker_results=[_success()],
        max_retries=0,
    )
    with pytest.raises(PlanningError, match="budget exhausted"):
        run_swarm("oauth", services=services)

    assert beads.closed == []
    assert any("gh" in e[1] or "PAT" in e[1] for e in beads.failed)


# ---------------------------------------------------------------------
# Repair-phase dispatch
# ---------------------------------------------------------------------


def test_repair_missing_worktree_path(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    task = _ref("bd-gone")
    beads = StubBeads(in_progress=(task,), ready_queue=[()])
    services, wt, git, pr, worker = _make_services(
        tmp_path, beads=beads
    )
    # No worktree directory exists → MissingWorktree repair.
    run_swarm("oauth", services=services)

    assert [e[0] for e in beads.failed] == ["bd-gone"]
    # No commit/push/PR from the repair phase for this finding.
    assert not any(c[0] == "commit_all" for c in git.calls)
    assert pr.calls == [] or all(
        c[0] != "open_pr" for c in pr.calls
    )


def test_repair_completion_pending_runs_commit_push_pr_label(
    tmp_path: Path,
) -> None:
    """Reconciliation-detected `completion-pending` repair tail runs
    commit + push + open_pr + mark_pr_open. close_task and
    cleanup are deferred to the merge-advancement sweep on a
    future invocation, mirroring `_run_single_task`'s
    Task-3 defer-close shape.

    Reconciliation classifies as `completion-pending` (not
    `…-with-pr`) because `find_open_pr_url_for_branch` returns
    None — no pre-existing PR on this branch."""
    _scratch_feature(tmp_path)
    task = _ref("bd-done", title="Complete me")
    beads = StubBeads(
        in_progress=(task,),
        ready_queue=[()],
        bodies={"bd-done": "final desc"},
    )
    # Create the worktree with a .task_complete sentinel.
    worktree = tmp_path / ".worktrees" / "oauth" / "bd-done"
    worktree.mkdir(parents=True)
    (worktree / ".task_complete").write_text("DONE\n")

    pr = StubPr()
    # After the repair phase opens a PR, the merge-advancement
    # sweep runs against the labelled task. Default StubPr URL
    # ends in /pull/1 → the sweep looks up PR #1, which we
    # configure to return OPEN so it leaves the task alone.
    pr.pr_states = {1: PrState(number=1, state="OPEN", url="x")}

    services, wt, git, _, worker = _make_services(
        tmp_path, beads=beads, pr=pr
    )
    run_swarm("oauth", services=services)

    # Repair tail ran commit + push + open_pr.
    git_steps = [c[0] for c in git.calls]
    assert "commit_all" in git_steps
    assert "push_branch" in git_steps
    assert any(c[0] == "open_pr" for c in pr.calls)
    # Task labelled; not closed; worktree NOT cleaned up.
    assert beads.pr_marked == [("bd-done", 1)]
    assert beads.closed == []
    assert not any(c[0] == "cleanup" for c in wt.calls)


def test_repair_completion_pending_with_pr_labels_and_leaves(
    tmp_path: Path,
) -> None:
    """Reconciliation-detected `completion-pending-with-pr`: PR
    already open for the task branch. Repair handler labels the
    task with the existing PR's number and leaves it
    in_progress; close + cleanup happen later via the
    merge-advancement sweep when the PR is observed as MERGED."""
    _scratch_feature(tmp_path)
    task = _ref("bd-has-pr")
    beads = StubBeads(in_progress=(task,), ready_queue=[()])
    worktree = tmp_path / ".worktrees" / "oauth" / "bd-has-pr"
    worktree.mkdir(parents=True)
    (worktree / ".task_complete").write_text("DONE\n")

    pr = StubPr()
    existing_url = "https://github.com/example/repo/pull/9"
    pr.find_open_pr_url_for_branch = lambda branch: existing_url  # type: ignore[assignment]
    # Merge-advancement sweep will see pr-9 — configure it OPEN
    # so it leaves the task alone after the repair label.
    pr.pr_states = {9: PrState(number=9, state="OPEN", url="x")}

    services, wt, git, _, worker = _make_services(
        tmp_path, beads=beads, pr=pr
    )
    run_swarm("oauth", services=services)

    # No new PR opened; task labelled with PR #9 (parsed from
    # the existing URL); not closed; worktree NOT cleaned up.
    assert not any(c[0] == "open_pr" for c in pr.calls)
    assert beads.pr_marked == [("bd-has-pr", 9)]
    assert beads.closed == []
    assert not any(c[0] == "cleanup" for c in wt.calls)


def test_repair_failure_pending_calls_fail_task(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    task = _ref("bd-bad")
    beads = StubBeads(in_progress=(task,), ready_queue=[()])
    worktree = tmp_path / ".worktrees" / "oauth" / "bd-bad"
    worktree.mkdir(parents=True)
    (worktree / ".task_failed").write_text("real worker blocker\n")

    services, *_ = _make_services(tmp_path, beads=beads)
    run_swarm("oauth", services=services)

    assert len(beads.failed) == 1
    assert "real worker blocker" in beads.failed[0][1]


def test_repair_stale_no_sentinels_halts(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    task = _ref("bd-stale")
    beads = StubBeads(in_progress=(task,))
    worktree = tmp_path / ".worktrees" / "oauth" / "bd-stale"
    worktree.mkdir(parents=True)  # present, but no sentinels

    services, *_ = _make_services(tmp_path, beads=beads)
    with pytest.raises(PlanningError, match="stale worktree"):
        run_swarm("oauth", services=services)


def test_repair_missing_worktree_exhausted_budget_halts_before_main_loop(
    tmp_path: Path,
) -> None:
    """A reconcile-detected exhausted failure must halt before fetch_ready."""
    _scratch_feature(tmp_path)
    task = _ref("bd-gone")
    # Pre-existing retry count at the budget ceiling → this fail
    # exhausts the budget.
    beads = StubBeads(
        in_progress=(task,),
        ready_queue=[(_ref("bd-next"),)],  # would be consumed if loop ran
        retries={"bd-gone": 1},
    )
    services, *_ = _make_services(tmp_path, beads=beads, max_retries=1)

    with pytest.raises(PlanningError, match="budget exhausted.*repair"):
        run_swarm("oauth", services=services)

    # Main loop never ran — no list_ready_tasks call, no claim, no
    # close.
    assert not any(c[0] == "list_ready_tasks" for c in beads.calls)
    assert not any(c[0] == "claim_task" for c in beads.calls)
    assert beads.closed == []
    # The failure was recorded before the halt fired.
    assert [e[0] for e in beads.failed] == ["bd-gone"]


def test_repair_failure_pending_exhausted_budget_halts_before_main_loop(
    tmp_path: Path,
) -> None:
    _scratch_feature(tmp_path)
    task = _ref("bd-bad")
    beads = StubBeads(
        in_progress=(task,),
        ready_queue=[(_ref("bd-next"),)],
        retries={"bd-bad": 1},
    )
    worktree = tmp_path / ".worktrees" / "oauth" / "bd-bad"
    worktree.mkdir(parents=True)
    (worktree / ".task_failed").write_text("fatal compile error\n")

    services, *_ = _make_services(tmp_path, beads=beads, max_retries=1)

    with pytest.raises(PlanningError, match="budget exhausted.*repair"):
        run_swarm("oauth", services=services)

    assert not any(c[0] == "list_ready_tasks" for c in beads.calls)
    assert beads.closed == []
    # The reconcile reason was prefixed and passed through to fail_task.
    assert "fatal compile error" in beads.failed[0][1]


def test_repair_collects_all_exhaustions_before_raising(
    tmp_path: Path,
) -> None:
    """Multiple repair-phase failures should all record before the halt."""
    _scratch_feature(tmp_path)
    missing, failing = _ref("bd-gone"), _ref("bd-bad")
    beads = StubBeads(
        in_progress=(missing, failing),
        ready_queue=[()],
        retries={"bd-gone": 1, "bd-bad": 1},
    )
    worktree = tmp_path / ".worktrees" / "oauth" / "bd-bad"
    worktree.mkdir(parents=True)
    (worktree / ".task_failed").write_text("reason\n")
    services, *_ = _make_services(tmp_path, beads=beads, max_retries=1)

    with pytest.raises(PlanningError, match="bd-gone.*bd-bad"):
        run_swarm("oauth", services=services)

    recorded_ids = [e[0] for e in beads.failed]
    assert recorded_ids == ["bd-gone", "bd-bad"]


def test_repair_orphan_branch_logs_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _scratch_feature(tmp_path)
    beads = StubBeads(ready_queue=[()])
    services, wt, git, pr, worker = _make_services(tmp_path, beads=beads)
    # Seed an orphan branch on the worktree stub.
    wt.list_task_branches = lambda feature: ("task/oauth/bd-orphan",)  # type: ignore[assignment]
    run_swarm("oauth", services=services)

    captured = capsys.readouterr()
    assert "bd-orphan" in captured.out
    assert not any(c[0] == "cleanup" for c in wt.calls)


# ---------------------------------------------------------------------
# Stale-sentinel clearing before worker invocation
# ---------------------------------------------------------------------


class _AssertSentinelsCleanWorker:
    """Worker stub that asserts both sentinels are absent at the moment
    `run()` is invoked. On success path it writes `.task_complete`
    afresh so the rest of the orchestrator continues normally.

    Used to pin the invariant that `_orchestrator._clear_sentinels` is
    applied after `worktree.setup` and before `worker.run`, not just
    before a ClaudeCodeWorker subprocess would have fired.
    """

    name = "stub-assert-clean"

    def __init__(self) -> None:
        self.invocations: list[WorkerInvocation] = []
        self.sentinels_present_at_run: list[str] = []

    def run(self, invocation: WorkerInvocation) -> WorkerResult:
        self.invocations.append(invocation)
        for sentinel in (".task_complete", ".task_failed"):
            if (invocation.worktree / sentinel).exists():
                self.sentinels_present_at_run.append(sentinel)
        (invocation.worktree / ".task_complete").write_text("DONE\n")
        return WorkerResult(
            status="success", reason="", stdout="", stderr=""
        )


def _services_with_worker(
    tmp_path: Path,
    beads: StubBeads,
    worker,
) -> tuple[SwarmServices, StubWorktree, StubGit, StubPr]:
    """Build services bound to a caller-provided worker instance.

    The shared `_make_services` helper always builds a `StubWorker`;
    the sentinel-clearing tests need a custom worker whose `run()`
    reads the worktree filesystem.
    """
    wt = StubWorktree(repo_root=tmp_path)
    git = StubGit()
    pr = StubPr()
    services = SwarmServices(
        beads=beads,  # type: ignore[arg-type]
        worktree=wt,  # type: ignore[arg-type]
        git=git,  # type: ignore[arg-type]
        pr=pr,  # type: ignore[arg-type]
        worker_factory=lambda: worker,  # type: ignore[return-value]
        repo_root=tmp_path,
    )
    return services, wt, git, pr


def test_clear_sentinels_removes_stale_both_before_worker_invocation(
    tmp_path: Path,
) -> None:
    """Retry against a kept worktree: both stale sentinels must be
    gone before the worker runs, and the orchestrator follows the
    happy path without re-reading them."""
    _scratch_feature(tmp_path)
    task = _ref("bd-1")
    beads = StubBeads(ready_queue=[(task,)])

    worktree = tmp_path / ".worktrees" / "oauth" / "bd-1"
    worktree.mkdir(parents=True)
    (worktree / ".task_complete").write_text("old success\n")
    (worktree / ".task_failed").write_text("old failure reason\n")

    worker = _AssertSentinelsCleanWorker()
    services, _wt, _git, _pr = _services_with_worker(
        tmp_path, beads, worker
    )

    run_swarm("oauth", services=services)

    # Neither sentinel was visible at the moment worker.run() fired.
    assert worker.sentinels_present_at_run == []
    # Happy path: task labelled `turma-pr:<N>` (mark_pr_open), no
    # fail_task contamination from the stale .task_failed reason.
    # close_task is deferred to the merge-advancement sweep.
    assert beads.pr_marked == [("bd-1", 1)]
    assert beads.closed == []
    assert beads.failed == []


def test_clear_sentinels_stale_failed_only_does_not_leak_into_fail_task(
    tmp_path: Path,
) -> None:
    """`.task_failed` left over from a prior attempt must not make
    the orchestrator re-report the failure when the worker itself
    succeeds this time."""
    _scratch_feature(tmp_path)
    task = _ref("bd-1")
    beads = StubBeads(ready_queue=[(task,)])

    worktree = tmp_path / ".worktrees" / "oauth" / "bd-1"
    worktree.mkdir(parents=True)
    (worktree / ".task_failed").write_text("lingering reason\n")

    worker = _AssertSentinelsCleanWorker()
    services, _wt, _git, _pr = _services_with_worker(
        tmp_path, beads, worker
    )

    run_swarm("oauth", services=services)

    assert worker.sentinels_present_at_run == []
    # Success path now ends at `mark_pr_open`; `close_task` is
    # deferred to the merge-advancement sweep.
    assert beads.pr_marked == [("bd-1", 1)]
    assert beads.closed == []
    assert beads.failed == []


def test_clear_sentinels_is_noop_on_fresh_worktree(
    tmp_path: Path,
) -> None:
    """First-attempt path: no sentinels present, `_clear_sentinels`
    must be a silent no-op (no FileNotFoundError bubbling up)."""
    _scratch_feature(tmp_path)
    task = _ref("bd-1")
    beads = StubBeads(ready_queue=[(task,)])
    services, _wt, _git, _pr, worker = _make_services(
        tmp_path, beads=beads, worker_results=[_success()]
    )

    # No sentinels, no worktree dir; StubWorktree.setup will mkdir.
    run_swarm("oauth", services=services)

    # Success path now ends at `mark_pr_open`; `close_task` is
    # deferred to the merge-advancement sweep.
    assert beads.pr_marked == [("bd-1", 1)]
    assert beads.closed == []
    assert beads.failed == []
    # Worker invoked exactly once; no error path entered.
    assert len(worker.invocations) == 1


# ---------------------------------------------------------------------
# Retry-path orphan-branch log suppression
# ---------------------------------------------------------------------


def test_repair_orphan_branch_log_suppressed_for_ready_task_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retry scenario: a failed-not-exhausted task is back in `ready`
    state and its persisted worktree branch still exists.

    Reconciliation correctly classifies it as `orphan-branch` per the
    v1 contract ("no in_progress task for this branch") — that
    classification is unchanged by this branch. But the orchestrator's
    repair-phase log line "repair: orphan branch (operator triage):
    ..." reads as misleading: the branch is about to be re-claimed by
    the main loop in this same run. The repair phase must suppress
    that specific log line when the branch matches a ready task id
    for the feature.
    """
    _scratch_feature(tmp_path)
    retry_task = _ref("bd-retry")
    beads = StubBeads(ready_queue=[(retry_task,)])
    services, wt, git, pr, worker = _make_services(
        tmp_path, beads=beads, worker_results=[_success()]
    )
    # The branch for the ready-but-about-to-be-claimed task is still
    # on disk from the prior failed attempt.
    wt.list_task_branches = lambda feature: (  # type: ignore[assignment]
        "task/oauth/bd-retry",
    )

    run_swarm("oauth", services=services)

    captured = capsys.readouterr()
    # Reconciliation itself still prints the classification line —
    # reconciliation.py is untouched in this branch.
    assert "task/oauth/bd-retry → orphan-branch" in captured.out
    # But the misleading operator-facing repair log line is suppressed.
    assert "orphan branch (operator triage)" not in captured.out
    # Main loop proceeded to claim + label the task; close happens
    # later via the merge-advancement sweep.
    assert beads.pr_marked == [("bd-retry", 1)]
    assert beads.closed == []


def test_repair_orphan_branch_still_logs_for_genuinely_orphaned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A branch with no corresponding ready or in_progress task is
    genuinely orphaned. The log must still fire so the operator sees
    it — suppression only applies to retry-ready matches."""
    _scratch_feature(tmp_path)
    # No tasks ready for this feature; the branch below is a real
    # orphan (leftover from a task that's closed or never existed).
    beads = StubBeads(ready_queue=[()])
    services, wt, git, pr, worker = _make_services(tmp_path, beads=beads)
    wt.list_task_branches = lambda feature: (  # type: ignore[assignment]
        "task/oauth/bd-genuinely-orphan",
    )

    run_swarm("oauth", services=services)

    captured = capsys.readouterr()
    assert (
        "repair: orphan branch (operator triage): "
        "task/oauth/bd-genuinely-orphan"
    ) in captured.out


def test_repair_orphan_branch_mixed_suppresses_only_retry_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two orphan-branch findings in one pass: one matches a ready
    task (suppressed), one is genuinely orphaned (logged). Verifies
    the suppression is applied per-finding, not all-or-nothing."""
    _scratch_feature(tmp_path)
    retry_task = _ref("bd-retry")
    beads = StubBeads(ready_queue=[(retry_task,)])
    services, wt, git, pr, worker = _make_services(
        tmp_path, beads=beads, worker_results=[_success()]
    )
    wt.list_task_branches = lambda feature: (  # type: ignore[assignment]
        "task/oauth/bd-retry",
        "task/oauth/bd-genuinely-orphan",
    )

    run_swarm("oauth", services=services)

    captured = capsys.readouterr()
    # Genuine orphan still logs.
    assert (
        "repair: orphan branch (operator triage): "
        "task/oauth/bd-genuinely-orphan"
    ) in captured.out
    # Retry-ready match is suppressed.
    assert (
        "repair: orphan branch (operator triage): "
        "task/oauth/bd-retry"
    ) not in captured.out


# ---------------------------------------------------------------------
# _pr_number_from_url
# ---------------------------------------------------------------------


def test_pr_number_from_url_canonical() -> None:
    assert (
        _pr_number_from_url(
            "https://github.com/turma-dev/turma/pull/42"
        )
        == 42
    )


def test_pr_number_from_url_canonical_with_trailing_slash() -> None:
    assert (
        _pr_number_from_url(
            "https://github.com/owner/repo/pull/7/"
        )
        == 7
    )


def test_pr_number_from_url_rejects_issue_url() -> None:
    """`/issues/<N>` is the wrong path segment — not a PR URL."""
    with pytest.raises(PlanningError, match="Could not parse"):
        _pr_number_from_url(
            "https://github.com/owner/repo/issues/42"
        )


def test_pr_number_from_url_rejects_non_https() -> None:
    """`gh pr create` only emits https URLs; an http URL is a
    contract violation, halt rather than guess."""
    with pytest.raises(PlanningError, match="Could not parse"):
        _pr_number_from_url(
            "http://github.com/owner/repo/pull/1"
        )


def test_pr_number_from_url_rejects_empty_string() -> None:
    with pytest.raises(PlanningError, match="Could not parse"):
        _pr_number_from_url("")


def test_pr_number_from_url_rejects_url_without_number() -> None:
    with pytest.raises(PlanningError, match="Could not parse"):
        _pr_number_from_url(
            "https://github.com/owner/repo/pull/"
        )


def test_pr_number_from_url_rejects_url_with_query_string() -> None:
    """`gh pr create` returns the URL on its own line without query
    params; if a future surface ever adds them the parser
    surfaces a `PlanningError` so the orchestrator halts before
    silently misrecording. Update the regex if/when this becomes
    a real case."""
    with pytest.raises(PlanningError, match="Could not parse"):
        _pr_number_from_url(
            "https://github.com/owner/repo/pull/1?tab=conversation"
        )


# ---------------------------------------------------------------------
# Merge-advancement phase
# ---------------------------------------------------------------------


def _ref_with_pr_label(
    task_id: str, pr_number: int, *, title: str = "t"
) -> BeadsTaskRef:
    """Build a BeadsTaskRef whose labels carry `turma-pr:<N>`. The
    merge-advancement sweep filters tasks by this label."""
    return BeadsTaskRef(
        id=task_id,
        title=title,
        labels=("feature:oauth", f"turma-pr:{pr_number}"),
    )


def _setup_labelled_task_for_advancement(
    tmp_path: Path,
    task_id: str,
    pr_number: int,
) -> None:
    """Create the on-disk state reconciliation expects for a
    labelled in_progress task to survive the repair phase intact:
    a worktree directory with `.task_complete` plus a stubbed
    `find_open_pr_url_for_branch` (set up by the caller) so
    reconciliation classifies as `completion-pending-with-pr` and
    the repair phase's label-and-leave handler runs idempotently
    (the label is already present), letting merge-advancement
    operate on the still-in_progress task."""
    worktree = tmp_path / ".worktrees" / "oauth" / task_id
    worktree.mkdir(parents=True)
    (worktree / ".task_complete").write_text("DONE\n")


def test_merge_advancement_merged_path(tmp_path: Path) -> None:
    """Single in_progress task with `turma-pr:5`, gh returns MERGED.
    Sweep unmarks the label, closes the bd task, and cleans the
    worktree — exactly the steps the prior success path used to
    fire at PR-open time, now relocated."""
    _scratch_feature(tmp_path)
    _setup_labelled_task_for_advancement(tmp_path, "bd-1", 5)
    task = _ref_with_pr_label("bd-1", 5)
    beads = StubBeads(in_progress=(task,))
    pr = StubPr(
        pr_states={
            5: PrState(
                number=5,
                state="MERGED",
                url="https://github.com/o/r/pull/5",
            )
        }
    )
    # Reconciliation classifies as completion-pending-with-pr;
    # repair phase labels (idempotent — already labelled);
    # merge-advancement then sees MERGED and closes.
    pr.find_open_pr_url_for_branch = lambda branch: "https://github.com/o/r/pull/5"  # type: ignore[assignment]

    services, wt, git, _, _ = _make_services(
        tmp_path, beads=beads, pr=pr
    )

    run_swarm("oauth", services=services)

    # bd: unmark → close, in that order.
    bd_call_order = [
        c[0] for c in beads.calls
        if c[0] in ("unmark_pr_open", "close_task")
    ]
    assert bd_call_order == ["unmark_pr_open", "close_task"]
    assert beads.pr_unmarked == [("bd-1", 5)]
    assert beads.closed == ["bd-1"]
    # Worktree cleaned up.
    assert any(c[0] == "cleanup" for c in wt.calls)
    # No fail_task fired.
    assert beads.failed == []


def test_merge_advancement_open_leaves_alone(tmp_path: Path) -> None:
    """`gh` returns OPEN — task stays in_progress, label intact,
    no Beads / worktree mutation. Main loop runs normally
    afterwards."""
    _scratch_feature(tmp_path)
    _setup_labelled_task_for_advancement(tmp_path, "bd-1", 5)
    task = _ref_with_pr_label("bd-1", 5)
    beads = StubBeads(in_progress=(task,))
    pr = StubPr(
        pr_states={
            5: PrState(
                number=5,
                state="OPEN",
                url="https://github.com/o/r/pull/5",
            )
        }
    )
    pr.find_open_pr_url_for_branch = lambda branch: "https://github.com/o/r/pull/5"  # type: ignore[assignment]

    services, wt, _, _, _ = _make_services(
        tmp_path, beads=beads, pr=pr
    )

    run_swarm("oauth", services=services)

    # Repair phase labels (idempotent), merge-advancement sees
    # OPEN → leave alone. No close, no fail, no cleanup.
    assert beads.pr_unmarked == []
    assert beads.closed == []
    assert beads.failed == []
    assert not any(c[0] == "cleanup" for c in wt.calls)


def test_merge_advancement_draft_pr_treated_as_open(
    tmp_path: Path,
) -> None:
    """`gh pr view --json state` returns "OPEN" for draft PRs;
    the v1 contract is "treat drafts as OPEN" (the adapter does
    not query `isDraft`). Pin that the merge-advancement sweep
    leaves draft-state tasks alone, identical to non-draft OPEN."""
    _scratch_feature(tmp_path)
    _setup_labelled_task_for_advancement(tmp_path, "bd-1", 5)
    task = _ref_with_pr_label("bd-1", 5)
    beads = StubBeads(in_progress=(task,))
    # `state == "OPEN"` mirrors what `gh` returns for both
    # drafts and non-drafts; the adapter contract is identical.
    pr = StubPr(
        pr_states={
            5: PrState(
                number=5,
                state="OPEN",
                url="https://github.com/o/r/pull/5",
            )
        }
    )
    pr.find_open_pr_url_for_branch = lambda branch: "https://github.com/o/r/pull/5"  # type: ignore[assignment]
    services, _, _, _, _ = _make_services(
        tmp_path, beads=beads, pr=pr
    )

    run_swarm("oauth", services=services)

    assert beads.pr_unmarked == []
    assert beads.closed == []
    assert beads.failed == []


def test_merge_advancement_closed_without_merge_with_budget_remaining(
    tmp_path: Path,
) -> None:
    """`gh` returns CLOSED — sweep unmarks the label and routes
    through `_handle_failure`. Budget remaining → task returns
    to `open` and becomes ready again on a future run."""
    _scratch_feature(tmp_path)
    _setup_labelled_task_for_advancement(tmp_path, "bd-1", 5)
    task = _ref_with_pr_label("bd-1", 5)
    beads = StubBeads(in_progress=(task,))
    pr = StubPr(
        pr_states={
            5: PrState(
                number=5,
                state="CLOSED",
                url="https://github.com/o/r/pull/5",
            )
        }
    )
    pr.find_open_pr_url_for_branch = lambda branch: "https://github.com/o/r/pull/5"  # type: ignore[assignment]
    services, _, _, _, _ = _make_services(
        tmp_path, beads=beads, pr=pr, max_retries=1
    )

    run_swarm("oauth", services=services)

    # unmark fired before fail_task (clean label first).
    bd_call_order = [
        c[0] for c in beads.calls
        if c[0] in ("unmark_pr_open", "fail_task")
    ]
    assert bd_call_order == ["unmark_pr_open", "fail_task"]
    # The reason landed on the bd task.
    assert beads.failed[0][1] == "PR #5 closed without merge"
    # Not closed (just labelled fail; budget remaining returns to
    # open via fail_task).
    assert beads.closed == []


def test_merge_advancement_closed_without_merge_exhausted_halts(
    tmp_path: Path,
) -> None:
    """CLOSED + already-at-budget-ceiling → halt run with a
    terminal `PlanningError` naming the task."""
    _scratch_feature(tmp_path)
    _setup_labelled_task_for_advancement(tmp_path, "bd-1", 5)
    task = _ref_with_pr_label("bd-1", 5)
    beads = StubBeads(
        in_progress=(task,),
        retries={"bd-1": 1},  # at the ceiling for max_retries=1
    )
    pr = StubPr(
        pr_states={
            5: PrState(
                number=5,
                state="CLOSED",
                url="https://github.com/o/r/pull/5",
            )
        }
    )
    pr.find_open_pr_url_for_branch = lambda branch: "https://github.com/o/r/pull/5"  # type: ignore[assignment]
    services, _, _, _, _ = _make_services(
        tmp_path, beads=beads, pr=pr, max_retries=1
    )

    with pytest.raises(
        PlanningError,
        match="budget exhausted on bd-1.*merge-advancement",
    ):
        run_swarm("oauth", services=services)

    assert beads.pr_unmarked == [("bd-1", 5)]


def test_merge_advancement_multi_task_sweep(tmp_path: Path) -> None:
    """Three labelled tasks across MERGED / OPEN / CLOSED. Each
    handler fires once in iteration order; the OPEN task is
    untouched; the run reaches `fetch_ready` (no exhaustion).

    Reconciliation classifies all three as
    `completion-pending-with-pr` — `find_open_pr_url_for_branch`
    must return a URL. The repair phase labels each (idempotent
    on the already-labelled tasks). Then merge-advancement
    dispatches per gh's reported state."""
    _scratch_feature(tmp_path)
    for tid, n in [("bd-merged", 1), ("bd-open", 2), ("bd-closed", 3)]:
        _setup_labelled_task_for_advancement(tmp_path, tid, n)
    t_merged = _ref_with_pr_label("bd-merged", 1)
    t_open = _ref_with_pr_label("bd-open", 2)
    t_closed = _ref_with_pr_label("bd-closed", 3)
    beads = StubBeads(
        in_progress=(t_merged, t_open, t_closed),
    )
    pr = StubPr(
        pr_states={
            1: PrState(number=1, state="MERGED", url="x"),
            2: PrState(number=2, state="OPEN", url="x"),
            3: PrState(number=3, state="CLOSED", url="x"),
        }
    )
    # Branch-keyed URL lookup so the repair phase labels each
    # task with its correct PR number.
    branch_to_url = {
        "task/oauth/bd-merged": "https://github.com/o/r/pull/1",
        "task/oauth/bd-open": "https://github.com/o/r/pull/2",
        "task/oauth/bd-closed": "https://github.com/o/r/pull/3",
    }
    pr.find_open_pr_url_for_branch = lambda b: branch_to_url[b]  # type: ignore[assignment]

    services, _, _, _, _ = _make_services(
        tmp_path, beads=beads, pr=pr, max_retries=1
    )

    run_swarm("oauth", services=services)

    # MERGED task: unmark + close. CLOSED task: unmark + fail.
    # OPEN task: untouched.
    assert beads.pr_unmarked == [
        ("bd-merged", 1),
        ("bd-closed", 3),
    ]
    assert beads.closed == ["bd-merged"]
    assert [e[0] for e in beads.failed] == ["bd-closed"]


def test_merge_advancement_404_halts_with_typed_error(
    tmp_path: Path,
) -> None:
    """Recorded PR number doesn't exist on GitHub. The sweep
    raises a typed `PlanningError` naming the task and PR
    number; the run halts before the main loop. No mutations
    to the task that triggered it.

    The test pre-labels the task in bd state directly and skips
    `find_open_pr_url_for_branch` (returning None) so
    reconciliation classifies as `completion-pending` rather
    than `completion-pending-with-pr` — the repair phase then
    runs `_complete_pending_task` which would open a NEW PR…
    which we don't want here. Easier: drop the worktree so
    reconciliation classifies as `missing-worktree`, repair
    fail_tasks the existing task. But we want
    merge-advancement to fire on the labelled-but-stale 404
    case. The cleanest is to inject the labelled task purely
    into the merge-advancement input (in_progress) without
    triggering reconciliation paths — possible by giving the
    task a worktree directory that survives reconciliation as
    `completion-pending-with-pr` against an existing PR URL,
    then have merge-advancement encounter a 404 on the
    *recorded* number (different from the find URL)."""
    _scratch_feature(tmp_path)
    _setup_labelled_task_for_advancement(tmp_path, "bd-stale", 9999)
    task = _ref_with_pr_label("bd-stale", 9999)
    beads = StubBeads(in_progress=(task,))
    pr = StubPr(pr_states={9999: None})  # explicit 404 trigger
    # find returns a different URL; reconciliation labels
    # with PR #1, not 9999. But the existing label is
    # turma-pr:9999 (from the BeadsTaskRef fixture); bd's
    # `--add-label` is idempotent, so the second label is
    # additive, not replacing. _extract_pr_number picks the
    # first match — `turma-pr:9999` (deterministic order).
    pr.find_open_pr_url_for_branch = lambda branch: "https://github.com/o/r/pull/1"  # type: ignore[assignment]

    services, _, _, _, _ = _make_services(
        tmp_path, beads=beads, pr=pr
    )

    with pytest.raises(
        PlanningError, match="PR #9999 for task bd-stale not found"
    ):
        run_swarm("oauth", services=services)

    # No `unmark_pr_open` for the failing task — the sweep
    # raises before mutation.
    assert ("bd-stale", 9999) not in beads.pr_unmarked
    assert beads.closed == []


def test_merge_advancement_dry_run_is_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """On `--dry-run`, the sweep queries PR state but performs no
    Beads / worktree mutations. Operator-facing log lines get a
    `would: ` prefix so the preview is unambiguous.

    Dry-run skips the repair phase entirely (existing behavior)
    so the labelled tasks reach merge-advancement directly. No
    fixture worktree / find_open_pr stubbing needed."""
    _scratch_feature(tmp_path)
    t_merged = _ref_with_pr_label("bd-merged", 1)
    t_closed = _ref_with_pr_label("bd-closed", 3)
    beads = StubBeads(in_progress=(t_merged, t_closed))
    pr = StubPr(
        pr_states={
            1: PrState(number=1, state="MERGED", url="x"),
            3: PrState(number=3, state="CLOSED", url="x"),
        }
    )
    services, wt, git, _, _ = _make_services(
        tmp_path, beads=beads, pr=pr
    )

    run_swarm("oauth", services=services, dry_run=True)

    # PR-state queries fired.
    assert ("get_pr_state_by_number", 1) in pr.calls
    assert ("get_pr_state_by_number", 3) in pr.calls
    # No mutations.
    assert beads.pr_unmarked == []
    assert beads.closed == []
    assert beads.failed == []
    assert not any(c[0] == "cleanup" for c in wt.calls)
    # `would: ` prefix appears in stdout for each mutating-class
    # finding.
    captured = capsys.readouterr()
    assert "would: merge-advancement: bd-merged → MERGED" in captured.out
    assert "would: merge-advancement: bd-closed → CLOSED" in captured.out
