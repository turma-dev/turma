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

    # Beads: claim, get_task_body (for invocation), close. No fail.
    ordered = [c[0] for c in beads.calls]
    assert "claim_task" in ordered
    assert "close_task" in ordered
    assert "fail_task" not in ordered

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

    # Worktree set up and cleaned up exactly once.
    assert [c[0] for c in wt.calls].count("setup") == 1
    assert [c[0] for c in wt.calls].count("cleanup") == 1


def test_multi_task_sequential_loop(tmp_path: Path) -> None:
    _scratch_feature(tmp_path)
    t1, t2 = _ref("bd-1"), _ref("bd-2")
    # Two iterations: first round yields (t1,), second yields (t2,).
    beads = StubBeads(ready_queue=[(t1,), (t2,)])
    services, wt, git, pr, worker = _make_services(
        tmp_path, beads=beads, worker_results=[_success(), _success()]
    )
    run_swarm("oauth", services=services)

    assert beads.closed == ["bd-1", "bd-2"]
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

    # Only bd-1 closed; loop stopped before bd-2.
    assert beads.closed == ["bd-1"]


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

    # Raced task was attempted (claim raised), winner closed normally.
    claim_ids = [c[1] for c in beads.calls if c[0] == "claim_task"]
    assert "bd-race" in claim_ids
    assert "bd-winner" in claim_ids
    assert beads.closed == ["bd-winner"]
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

    # One failure recorded, one successful close.
    assert [e[1] for e in beads.failed] == ["timeout"]
    assert beads.closed == ["bd-1"]


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


def test_repair_completion_pending_runs_commit_push_pr_close(
    tmp_path: Path,
) -> None:
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

    services, wt, git, pr, worker = _make_services(
        tmp_path, beads=beads
    )
    run_swarm("oauth", services=services)

    git_steps = [c[0] for c in git.calls]
    assert "commit_all" in git_steps
    assert "push_branch" in git_steps
    assert [c[0] for c in pr.calls] == [
        "find_open_pr_url_for_branch",  # reconciliation's lookup
        "open_pr",                      # repair-phase tail
    ]
    assert beads.closed == ["bd-done"]


def test_repair_completion_pending_with_pr_closes_and_cleans(
    tmp_path: Path,
) -> None:
    _scratch_feature(tmp_path)
    task = _ref("bd-has-pr")
    beads = StubBeads(in_progress=(task,), ready_queue=[()])
    worktree = tmp_path / ".worktrees" / "oauth" / "bd-has-pr"
    worktree.mkdir(parents=True)
    (worktree / ".task_complete").write_text("DONE\n")

    pr = StubPr(url="https://github.com/example/repo/pull/9")
    # Pre-existing PR for this branch → PR lookup returns a URL.
    pr.url = "https://github.com/example/repo/pull/9"
    # Hack: StubPr's find method returns None by default; flip it by
    # monkeypatching to return a URL for this branch.
    existing_url = "https://github.com/example/repo/pull/9"
    pr.find_open_pr_url_for_branch = lambda branch: existing_url  # type: ignore[assignment]

    services, wt, git, _, worker = _make_services(
        tmp_path, beads=beads, pr=pr
    )
    run_swarm("oauth", services=services)

    # No new PR opened; task closed; worktree cleaned up.
    assert not any(c[0] == "open_pr" for c in pr.calls)
    assert beads.closed == ["bd-has-pr"]
    assert any(c[0] == "cleanup" for c in wt.calls)


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
    # Happy path: task closed, no fail_task contamination from the
    # stale .task_failed reason.
    assert beads.closed == ["bd-1"]
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
    assert beads.closed == ["bd-1"]
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

    assert beads.closed == ["bd-1"]
    assert beads.failed == []
    # Worker invoked exactly once; no error path entered.
    assert len(worker.invocations) == 1
