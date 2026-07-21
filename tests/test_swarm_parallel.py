"""Task 1 of ``swarm-parallel-multi-pool``: pin the concurrency and
serialization invariants as failing tests, before the dispatcher exists.

Two layers:

- **Router / config (green now).** ``turma.swarm.pools`` is pure and
  implemented, so its routing + validation tests pass.
- **Concurrency invariants (red now).** These drive the not-yet-implemented
  ``dispatch_concurrent`` seam, which raises ``NotImplementedError`` — so each
  fails on "not built yet," not on a missing symbol or a trivial pass. The
  bodies use interval-recording fakes so that once the dispatcher + one global
  mutation lock land (Tasks 3-4) the assertions become meaningful:

    1. no two Beads-DB (``bd``) calls overlap,
    2. no two shared-``.git`` worktree/branch ops overlap,
    3. at least two workers run concurrently (outside the lock),
    4. pool caps and ``max_parallel`` bound concurrency,
    5. a halting failure drains in-flight workers rather than cancelling them.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from turma.errors import PlanningError
from turma.swarm._orchestrator import SwarmServices
from turma.swarm.dispatch import dispatch_concurrent
from turma.swarm.pools import Pool, PoolRouter, build_router
from turma.swarm.worker import WorkerInvocation, WorkerResult
from turma.swarm.worktree import WorktreeRef
from turma.transcription.beads import BeadsTaskRef


# =====================================================================
# Router / config — implemented, green
# =====================================================================


def _pool(name, backend, types, *, max=1, default=False):
    return Pool(name=name, backend=backend, types=tuple(types), max=max,
                default=default)


def test_router_routes_by_task_type():
    router = build_router([
        _pool("anthropic", "claude-code", ["impl", "spec"], max=2, default=True),
        _pool("openai", "codex", ["test", "docs"], max=2),
    ])
    assert router.pool_for("impl").name == "anthropic"
    assert router.pool_for("test").name == "openai"
    assert router.pool_for("docs").backend == "codex"


def test_router_falls_back_to_default_pool():
    router = build_router([
        _pool("anthropic", "claude-code", ["impl"], default=True),
        _pool("openai", "codex", ["test"]),
    ])
    # An unmatched type routes to the single default pool.
    assert router.pool_for("chore").name == "anthropic"


def test_build_router_rejects_duplicate_types():
    with pytest.raises(PlanningError, match="at most one pool"):
        build_router([
            _pool("a", "claude-code", ["impl"], default=True),
            _pool("b", "codex", ["impl"]),  # duplicate 'impl'
        ])


@pytest.mark.parametrize("defaults", [0, 2])
def test_build_router_requires_exactly_one_default(defaults):
    pools = [
        _pool("a", "claude-code", ["impl"], default=defaults >= 1),
        _pool("b", "codex", ["test"], default=defaults >= 2),
    ]
    with pytest.raises(PlanningError, match="exactly one pool"):
        build_router(pools)


def test_build_router_rejects_max_below_one():
    with pytest.raises(PlanningError, match="max must be >= 1"):
        build_router([_pool("a", "claude-code", ["impl"], max=0, default=True)])


def test_build_router_rejects_empty():
    with pytest.raises(PlanningError, match="at least one pool"):
        build_router([])


# =====================================================================
# Interval-recording fakes for the concurrency invariants
# =====================================================================


class Recorder:
    """Thread-safe interval log; detects overlapping operations and peak
    concurrency, keyed by an op-name prefix."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.intervals: list[tuple[str, float, float]] = []
        self._live: dict[str, int] = {}
        self.peak: dict[str, int] = {}

    @contextlib.contextmanager
    def track(self, op: str, prefix: str, hold: float = 0.02):
        start = time.monotonic()
        with self._lock:
            self._live[prefix] = self._live.get(prefix, 0) + 1
            self.peak[prefix] = max(self.peak.get(prefix, 0), self._live[prefix])
        try:
            time.sleep(hold)  # a window in which an overlap could be observed
            yield
        finally:
            end = time.monotonic()
            with self._lock:
                self._live[prefix] -= 1
                self.intervals.append((op, start, end))

    def overlapping(self, prefix: str) -> list[tuple[str, str]]:
        ivals = sorted(
            (s, e, op) for (op, s, e) in self.intervals if op.startswith(prefix)
        )
        bad = []
        for i in range(len(ivals)):
            for j in range(i + 1, len(ivals)):
                if ivals[j][0] < ivals[i][1]:  # j starts before i ends → overlap
                    bad.append((ivals[i][2], ivals[j][2]))
        return bad


@dataclass
class RecordingBeads:
    rec: Recorder
    ready: list[BeadsTaskRef] = field(default_factory=list)
    completed: set[str] = field(default_factory=set)

    def _bd(self, op):
        return self.rec.track(f"bd:{op}", "bd")

    def list_ready_tasks(self, feature):
        with self._bd("list_ready_tasks"):
            return tuple(t for t in self.ready if t.id not in self.completed)

    def list_in_progress_tasks(self, feature):
        with self._bd("list_in_progress_tasks"):
            return ()

    def get_task_body(self, task_id):
        with self._bd("get_task_body"):
            return "body"

    def retries_so_far(self, task_id):
        with self._bd("retries_so_far"):
            return 0

    def claim_task(self, task_id):
        with self._bd("claim_task"):
            self.completed.add(task_id)  # claimed → no longer ready

    def mark_pr_open(self, task_id, pr_number):
        with self._bd("mark_pr_open"):
            pass

    def close_task(self, task_id):
        with self._bd("close_task"):
            pass

    def fail_task(self, task_id, reason, *, retries_so_far, max_retries):
        with self._bd("fail_task"):
            pass


@dataclass
class RecordingWorktree:
    rec: Recorder
    repo_root: Path

    def worktree_path_for(self, feature, task_id):
        return self.repo_root / ".worktrees" / feature / task_id

    def branch_name_for(self, feature, task_id):
        return f"task/{feature}/{task_id}"

    def list_task_branches(self, feature):
        return ()

    def setup(self, *, feature, task_id, base_branch):
        # `git worktree add -b <branch>` — a shared-.git metadata op.
        with self.rec.track("git:worktree_add", "git"):
            path = self.repo_root / ".worktrees" / feature / task_id
            path.mkdir(parents=True, exist_ok=True)
            return WorktreeRef(path=path, branch=f"task/{feature}/{task_id}")

    def cleanup(self, ref):
        with self.rec.track("git:worktree_remove", "git"):
            pass


@dataclass
class RecordingGit:
    rec: Recorder

    def status_is_dirty(self, worktree):
        return True

    def commit_all_with_bd_export(self, worktree, message, *, beads, repo_root):
        with self.rec.track("git:commit", "git"):
            return "deadbeef"

    def push_branch(self, worktree, branch, *, remote="origin"):
        return None  # network op — outside the lock by design

    def fetch_and_ff_base(self, repo_root, base_branch):
        return None

    def path_is_dirty(self, repo_root, path):
        return False

    def revert_paths(self, repo_root, paths):
        return None


@dataclass
class RecordingPr:
    rec: Recorder

    def open_pr(self, *, branch, base, title, body):
        return "https://github.com/turma-dev/turma/pull/1"

    def find_open_pr_url_for_branch(self, branch):
        return None


@dataclass
class ControllableWorker:
    rec: Recorder
    delay: float = 0.05
    fail_ids: set[str] = field(default_factory=set)
    finished: list[str] = field(default_factory=list)
    barrier: "threading.Barrier | None" = None

    name: str = "recording"

    def run(self, invocation: WorkerInvocation) -> WorkerResult:
        with self.rec.track(f"worker:{invocation.task_id}", "worker", hold=0):
            # Deterministic overlap (vs sleep-timing): block until `parties`
            # workers are concurrently here. Only a truly concurrent dispatcher
            # releases the barrier; a serial one times out (BrokenBarrierError).
            if self.barrier is not None and invocation.task_id not in self.fail_ids:
                try:
                    self.barrier.wait(timeout=5)
                except threading.BrokenBarrierError:
                    pass
            time.sleep(self.delay)
            self.finished.append(invocation.task_id)  # normal terminal reached
            if invocation.task_id in self.fail_ids:
                return WorkerResult(
                    status="failure", reason="deliberate", stdout="", stderr=""
                )
            return WorkerResult(
                status="success", reason="", stdout="", stderr=""
            )


def _ref(task_id, turma_type="impl"):
    return BeadsTaskRef(
        id=task_id,
        title=task_id,
        labels=("feature:oauth", f"turma-type:{turma_type}"),
    )


def _services(tmp_path, beads, rec, worker_for, *, max_retries=1):
    """Build SwarmServices with recording adapters and a backend-keyed worker
    resolver (`worker_for`: pool.backend name -> worker) — the multi-pool DI
    shape. `worker_factory` (the sequential path) is set but unused here."""
    return SwarmServices(
        beads=beads,          # type: ignore[arg-type]
        worktree=RecordingWorktree(rec, tmp_path),  # type: ignore[arg-type]
        git=RecordingGit(rec),                       # type: ignore[arg-type]
        pr=RecordingPr(rec),                         # type: ignore[arg-type]
        worker_factory=lambda: worker_for("claude-code"),  # type: ignore[return-value]
        worker_for=worker_for,   # type: ignore[arg-type]
        repo_root=tmp_path,
        base_branch="main",
        max_retries=max_retries,
    )


def _one(worker):
    """Single-backend resolver: every backend name resolves to `worker`."""
    return lambda backend: worker


def _one_default_router(max=3):
    return build_router([
        _pool("anthropic", "claude-code", ["impl"], max=max, default=True),
    ])


# =====================================================================
# Concurrency invariants — red until the dispatcher + lock exist
# =====================================================================
#
# The concurrent dispatcher + one global mutation lock are now implemented, so
# these run live against it.


def test_no_overlapping_beads_calls(tmp_path):
    """Invariant 1: the global mutation lock serializes every `bd` call, so
    no two BeadsAdapter subprocess calls ever overlap under concurrency."""
    rec = Recorder()
    beads = RecordingBeads(rec, ready=[_ref(f"t{i}") for i in range(4)])
    services = _services(tmp_path, beads, rec, _one(ControllableWorker(rec)))
    dispatch_concurrent("oauth", services,
                        router=_one_default_router(), max_parallel=3)
    assert rec.overlapping("bd:") == []


def test_no_overlapping_git_metadata_ops(tmp_path):
    """Invariant 2: worktree add/remove + branch ops mutate the shared parent
    `.git`, so they must also serialize — no two overlap under concurrency."""
    rec = Recorder()
    beads = RecordingBeads(rec, ready=[_ref(f"t{i}") for i in range(4)])
    services = _services(tmp_path, beads, rec, _one(ControllableWorker(rec)))
    dispatch_concurrent("oauth", services,
                        router=_one_default_router(), max_parallel=3)
    assert rec.overlapping("git:") == []


def test_two_workers_run_concurrently_outside_the_lock(tmp_path):
    """Invariant 3: worker execution is NOT serialized — with slots free, at
    least two workers run at once (the whole point of concurrency)."""
    rec = Recorder()
    beads = RecordingBeads(rec, ready=[_ref(f"t{i}") for i in range(4)])
    worker = ControllableWorker(rec, delay=0.05, barrier=threading.Barrier(2))
    services = _services(tmp_path, beads, rec, _one(worker))
    dispatch_concurrent("oauth", services,
                        router=_one_default_router(max=3), max_parallel=3)
    assert rec.peak.get("worker", 0) >= 2


def test_pool_cap_binds(tmp_path):
    """Invariant 4a: a pool capped at 1 never runs two of its tasks at once,
    even with a generous max_parallel."""
    rec = Recorder()
    beads = RecordingBeads(rec, ready=[_ref(f"t{i}") for i in range(6)])
    worker = ControllableWorker(rec, delay=0.05)
    services = _services(tmp_path, beads, rec, _one(worker))
    dispatch_concurrent("oauth", services,
                        router=_one_default_router(max=1), max_parallel=5)
    assert rec.peak.get("worker", 0) == 1


def test_max_parallel_binds_below_summed_pool_caps(tmp_path):
    """Invariant 4b: total concurrency never exceeds max_parallel even when the
    pools' summed caps are larger. A dispatcher that honored only per-pool
    semaphores (ignoring the global slot limit) would wrongly pass, so the pools
    are capped at 3+3=6 while max_parallel=2 must bind."""
    rec = Recorder()
    ready = (
        [_ref(f"i{i}", "impl") for i in range(4)]
        + [_ref(f"t{i}", "test") for i in range(4)]
    )
    beads = RecordingBeads(rec, ready=ready)
    worker = ControllableWorker(rec, delay=0.05, barrier=threading.Barrier(2))
    router = build_router([
        _pool("anthropic", "claude-code", ["impl"], max=3, default=True),
        _pool("openai", "codex", ["test"], max=3),
    ])
    services = _services(tmp_path, beads, rec, _one(worker))
    dispatch_concurrent("oauth", services, router=router, max_parallel=2)
    assert rec.peak.get("worker", 0) == 2  # reaches, and never exceeds, the cap


def test_task_type_routes_to_its_pool_backend(tmp_path):
    """Core contract: a task's turma-type selects its pool, and the pool's
    backend runs it. `impl` -> the Claude pool/backend, `test` -> the Codex
    pool/backend. A dispatcher that ignored `pool.backend` (one shared worker)
    would fail this."""
    rec = Recorder()
    impl_ids = {f"i{i}" for i in range(3)}
    test_ids = {f"t{i}" for i in range(3)}
    ready = (
        [_ref(i, "impl") for i in impl_ids]
        + [_ref(t, "test") for t in test_ids]
    )
    beads = RecordingBeads(rec, ready=ready)
    claude = ControllableWorker(rec, delay=0.01, name="claude-code")
    codex = ControllableWorker(rec, delay=0.01, name="codex")
    workers = {"claude-code": claude, "codex": codex}
    services = _services(tmp_path, beads, rec, lambda backend: workers[backend])
    router = build_router([
        _pool("anthropic", "claude-code", ["impl"], max=3, default=True),
        _pool("openai", "codex", ["test"], max=3),
    ])
    dispatch_concurrent("oauth", services, router=router, max_parallel=4)
    assert set(claude.finished) == impl_ids  # Claude backend ran the impl tasks
    assert set(codex.finished) == test_ids   # Codex backend ran the test tasks


def test_failure_halt_drains_in_flight_not_cancel(tmp_path):
    """Invariant 5: a halting failure stops scheduling new slots but lets
    in-flight workers reach their normal terminal — drain, not cancel."""
    rec = Recorder()
    beads = RecordingBeads(rec, ready=[_ref("boom"), _ref("slow")])
    # 'boom' fails (drives the halt); 'slow' is mid-flight and must still reach
    # its terminal (recorded in worker.finished), not be cancelled.
    worker = ControllableWorker(rec, delay=0.1, fail_ids={"boom"})
    # max_retries=0 so 'boom' exhausts its budget on the first failure and halts
    services = _services(tmp_path, beads, rec, _one(worker), max_retries=0)
    with pytest.raises(PlanningError):
        dispatch_concurrent("oauth", services,
                            router=_one_default_router(max=2), max_parallel=2)
    # Drain, not cancel: every worker that started reached its normal terminal.
    assert "slow" in worker.finished
