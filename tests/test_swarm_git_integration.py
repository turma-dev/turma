"""Real-git integration tests for `GitAdapter` — the parts where
behavior depends on actual git (and, for the worker-commit boundary
protocol, actual bd) rather than the adapter's claimed contract.

Shells out to the actual `git` binary against tmpdir bare remote +
working clone fixtures. The subprocess-mock tests in
`test_swarm_git.py` validate the adapter's claimed contract against
itself; these tests validate the underlying tools' actual behavior
— exactly the gap that let the prior colon-form `fetch_and_ff_base`
contract ship past code review before the 2026-04-26 live smoke
caught it, and exactly where the wrong-path worker commit
(swarm-worker-commit-bd-ownership) hides if the unit tests alone
are trusted.

See `openspec/changes/swarm-fetch-and-ff-base-correction/` and
`openspec/changes/swarm-worker-commit-bd-ownership/` for the
contracts this module backs with real-tool evidence.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from turma.errors import PlanningError
from turma.swarm.git import GitAdapter
from turma.transcription.beads import BeadsAdapter


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not on PATH",
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git subcommand in `cwd` with deterministic config.

    Inline `-c` flags pin user identity and disable gpg-signing so
    the test is independent of the operator's global git config.
    """
    full_args = [
        "git",
        "-c", "user.name=Test",
        "-c", "user.email=test@example.com",
        "-c", "commit.gpgsign=false",
        *args,
    ]
    return subprocess.run(
        full_args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _rev_parse(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def _make_bare_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Build a tmpdir bare remote + a working clone with `main`
    checked out and a single seed commit pushed.

    Returns (bare_remote_path, working_clone_path).
    """
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--initial-branch=main")

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "--initial-branch=main")
    _git(clone, "remote", "add", "origin", str(bare))

    (clone / "README.md").write_text("seed\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-m", "seed")
    _git(clone, "push", "-u", "origin", "main")

    return bare, clone


def _push_remote_commit(
    bare: Path, tmp_path: Path, name: str
) -> str:
    """Push a new commit to `bare` via a second working clone.
    Returns the new commit SHA on origin/main."""
    other = tmp_path / f"other-{name}"
    other.mkdir()
    _git(other, "clone", str(bare), ".")
    (other / f"{name}.txt").write_text(f"{name}\n")
    _git(other, "add", f"{name}.txt")
    _git(other, "commit", "-m", name)
    _git(other, "push", "origin", "main")
    return _rev_parse(other, "HEAD")


def test_fetch_and_ff_base_happy_path_against_real_git(
    tmp_path: Path,
) -> None:
    """The exact case the live smoke caught: working clone has
    main checked out (standard `turma run` setup). A new commit
    lands on origin/main via a separate clone. fetch_and_ff_base
    advances local main without git refusing the destination."""
    bare, clone = _make_bare_and_clone(tmp_path)
    new_tip = _push_remote_commit(bare, tmp_path, "remote-commit")

    pre_local = _rev_parse(clone, "HEAD")
    assert pre_local != new_tip

    GitAdapter().fetch_and_ff_base(clone, "main")

    post_local = _rev_parse(clone, "HEAD")
    assert post_local == new_tip


def test_fetch_and_ff_base_divergent_local_raises_typed_error(
    tmp_path: Path,
) -> None:
    """Working clone has a local commit Y on main (never pushed).
    Bare remote separately gets commit Z. The fetch picks up Z
    into refs/remotes/origin/main; the merge --ff-only refuses
    because local has Y origin doesn't. Typed PlanningError
    naming the branch."""
    bare, clone = _make_bare_and_clone(tmp_path)

    (clone / "local-Y.txt").write_text("Y\n")
    _git(clone, "add", "local-Y.txt")
    _git(clone, "commit", "-m", "local Y")

    _push_remote_commit(bare, tmp_path, "remote-Z")

    with pytest.raises(PlanningError) as exc:
        GitAdapter().fetch_and_ff_base(clone, "main")

    msg = str(exc.value)
    assert "diverged" in msg.lower()
    assert "main" in msg


def test_fetch_and_ff_base_head_on_feature_does_not_corrupt_feature_ref(
    tmp_path: Path,
) -> None:
    """The silent-corruption case the precheck closes. HEAD on a
    feature branch that's an ancestor of origin/main. Without the
    precheck, `git merge --ff-only origin/main` from this state
    would silently advance the feature ref to origin's tip. With
    the precheck, the adapter refuses cleanly BEFORE any remote
    I/O and the feature ref is untouched."""
    bare, clone = _make_bare_and_clone(tmp_path)
    _push_remote_commit(bare, tmp_path, "remote-tip")

    _git(clone, "checkout", "-b", "feature")
    pre_feature_sha = _rev_parse(clone, "feature")

    with pytest.raises(PlanningError) as exc:
        GitAdapter().fetch_and_ff_base(clone, "main")

    msg = str(exc.value)
    assert "feature" in msg
    assert "main" in msg
    assert "cd" in msg.lower()

    # Critical assertion: feature ref unchanged. This is the
    # regression contract for the silent-corruption case.
    post_feature_sha = _rev_parse(clone, "feature")
    assert post_feature_sha == pre_feature_sha


def test_revert_then_fetch_and_ff_base_against_real_git(
    tmp_path: Path,
) -> None:
    """The regression contract for the 2026-04-26 iter-2 smoke
    finding (swarm-beads-state-merge-cleanliness): a tracked
    file gets dirty locally (simulating bd's post-update hook
    writing AND staging `.beads/issues.jsonl`); the orchestrator
    reverts; `fetch_and_ff_base` then succeeds even though the
    incoming origin commit modifies the same file.

    Without the revert, `merge --ff-only` would refuse with
    `Your local changes to the following files would be
    overwritten by merge`. This test pins the
    revert-fetch-merge sequence end-to-end against actual git.
    """
    bare, clone = _make_bare_and_clone(tmp_path)

    # Place a `.beads/issues.jsonl` at version V0 in both clone
    # and bare remote. Use a placeholder shape — doesn't have to
    # be real bd output; we're testing git's response to a
    # tracked file's working-tree state, not bd semantics.
    (clone / ".beads").mkdir()
    (clone / ".beads" / "issues.jsonl").write_text(
        '{"id":"task-A","status":"open"}\n'
    )
    _git(clone, "add", ".beads/issues.jsonl")
    _git(clone, "commit", "-m", "seed bd state V0")
    _git(clone, "push", "origin", "main")

    # Locally modify AND stage the file (simulating bd's
    # `export.auto=true` + `export.git-add=true` behavior on a
    # `bd update`).
    (clone / ".beads" / "issues.jsonl").write_text(
        '{"id":"task-A","status":"in_progress"}\n'
    )
    _git(clone, "add", ".beads/issues.jsonl")
    # Deliberately NOT commit. Tree now has a staged change.

    # Push a new commit to bare remote that ALSO touches
    # .beads/issues.jsonl with a DIFFERENT content (simulating
    # a worker commit captured in a PR merge).
    other = tmp_path / "other-remote"
    other.mkdir()
    _git(other, "clone", str(bare), ".")
    (other / ".beads" / "issues.jsonl").write_text(
        '{"id":"task-A","status":"in_progress","labels":["turma-pr:7"]}\n'
    )
    _git(other, "add", ".beads/issues.jsonl")
    _git(other, "commit", "-m", "worker commit V1")
    _git(other, "push", "origin", "main")

    # PRECONDITION: working clone's tree is dirty for
    # .beads/issues.jsonl. Without the revert, the next step's
    # `fetch_and_ff_base` would refuse. We've reproduced the
    # exact iter-2 smoke failure setup.
    pre_status = _git(clone, "status", "--porcelain=v1").stdout
    assert ".beads/issues.jsonl" in pre_status

    # STEP 1: orchestrator reverts the bd-state export.
    GitAdapter().revert_paths(clone, (".beads/issues.jsonl",))

    # Working tree clean now.
    post_revert_status = _git(clone, "status", "--porcelain=v1").stdout
    assert post_revert_status == ""

    # STEP 2: fetch_and_ff_base succeeds against real git.
    GitAdapter().fetch_and_ff_base(clone, "main")

    # Local main's HEAD matches origin's tip.
    post_fetch_head = _rev_parse(clone, "HEAD")
    post_fetch_origin = _rev_parse(clone, "origin/main")
    assert post_fetch_head == post_fetch_origin

    # Working tree still clean (the merge applied origin's V1
    # cleanly).
    post_fetch_status = _git(clone, "status", "--porcelain=v1").stdout
    assert post_fetch_status == ""

    # AND the file content reflects origin's V1 (the worker's
    # version), not the locally-discarded "in_progress" version.
    final_content = (clone / ".beads" / "issues.jsonl").read_text()
    assert "turma-pr:7" in final_content


def test_path_is_dirty_against_real_git(tmp_path: Path) -> None:
    """Companion to the revert + fetch test: pin that
    `path_is_dirty` correctly distinguishes clean / unstaged-
    modified / staged-modified / untracked against real git's
    `status --porcelain=v1` output. All four cases query the
    SAME path (`.beads/issues.jsonl`) — the contract is
    specifically about how this path's working-tree state maps
    to True/False, including the case where the path exists
    untracked rather than as a tracked file."""
    _, clone = _make_bare_and_clone(tmp_path)
    adapter = GitAdapter()

    # Untracked: file exists at the path but was never `git
    # add`-ed. `git status --porcelain=v1` emits `?? <path>`.
    # Per the contract, an untracked file at the bd-state path
    # is the operator's business, not Turma's — returns False.
    (clone / ".beads").mkdir()
    (clone / ".beads" / "issues.jsonl").write_text("untracked\n")
    assert adapter.path_is_dirty(clone, ".beads/issues.jsonl") is False

    # Tracked + clean: commit it. No changes since HEAD.
    _git(clone, "add", ".beads/issues.jsonl")
    _git(clone, "commit", "-m", "seed bd state")
    assert adapter.path_is_dirty(clone, ".beads/issues.jsonl") is False

    # Unstaged modification (` M` prefix in porcelain).
    (clone / ".beads" / "issues.jsonl").write_text("V1\n")
    assert adapter.path_is_dirty(clone, ".beads/issues.jsonl") is True

    # Staged modification (`M ` prefix in porcelain).
    _git(clone, "add", ".beads/issues.jsonl")
    assert adapter.path_is_dirty(clone, ".beads/issues.jsonl") is True


# ---------------------------------------------------------------------
# commit_worker_changes — real git + real bd
# (swarm-bd-export-serialization; code-only task commits)
# ---------------------------------------------------------------------


needs_bd = pytest.mark.skipif(
    shutil.which("bd") is None,
    reason="bd binary not on PATH",
)


_BD_PRE_COMMIT_HOOK_SHIM = """\
#!/usr/bin/env sh
# Test fixture — minimal bd pre-commit shim that mimics what
# `bd init` installs. Used by the negative-control test to
# reproduce the upstream bd defect's wrong-path commit shape.
if command -v bd >/dev/null 2>&1; then
  export BD_GIT_HOOK=1
  _bd_timeout=${BEADS_HOOK_TIMEOUT:-30}
  if command -v timeout >/dev/null 2>&1; then
    timeout "$_bd_timeout" bd hooks run pre-commit "$@"
    _bd_exit=$?
    if [ $_bd_exit -eq 124 ]; then
      _bd_exit=0
    fi
  else
    bd hooks run pre-commit "$@"
    _bd_exit=$?
  fi
  if [ $_bd_exit -eq 3 ]; then
    _bd_exit=0
  fi
  exit $_bd_exit
fi
exit 0
"""


def _make_bd_init_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Build a tmpdir bare remote + working clone with bd
    initialized AND `.beads/issues.jsonl` tracked at HEAD.

    Uses `bd init --skip-hooks` so bd's own auto-commit at init
    time does not fire the bd pre-commit hook (avoiding the
    documented `bd init` deadlock where the hook re-acquires
    bd's own lock). With `--skip-hooks`, bd init's auto-commit
    succeeds cleanly because no hook is installed to fire.

    bd init does NOT auto-create `.beads/issues.jsonl` (the
    JSONL export is generated lazily on the first bd write or
    via explicit `bd export`). The reproducer's bug shape
    requires `.beads/issues.jsonl` to be tracked at HEAD — so
    after init this helper runs `bd export` and commits the
    resulting file.

    Returns (bare_remote_path, working_clone_path).
    """
    bare, clone = _make_bare_and_clone(tmp_path)

    # Run `bd init --skip-hooks` non-interactively from inside
    # the clone so bd creates its `.beads/` directory there.
    env = {
        **os.environ,
        "BD_NON_INTERACTIVE": "1",
    }
    subprocess.run(
        [
            "bd", "init",
            "--skip-hooks",
            "--prefix", "probe",
        ],
        cwd=clone,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # bd init auto-commits its initial artifacts but NOT
    # `.beads/issues.jsonl` (the JSONL export is created by
    # bd's first write or by an explicit `bd export`, and bd
    # `export` from an empty db skips writing the file when
    # there are zero issues). Create one bd issue so the
    # auto-export populates `.beads/issues.jsonl`, then commit
    # it so HEAD tracks the file — the upstream defect's
    # reproducer requires that.
    subprocess.run(
        [
            "bd", "create",
            "--type", "task",
            "--priority", "2",
            "--description", "fixture seed",
            "--silent",
            "fixture seed",
        ],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _git(clone, "add", ".beads/issues.jsonl")
    _git(
        clone,
        "-c", "core.hooksPath=/dev/null",
        "commit", "-m", "track .beads/issues.jsonl",
    )

    return bare, clone


def _install_bd_pre_commit_hook(clone: Path) -> None:
    """Install bd's pre-commit hook shim into the clone's
    `.git/hooks/pre-commit`. Used by the negative-control test
    to reproduce the upstream bd defect's wrong-path commit
    shape — the defect requires the pre-commit hook to fire on
    a `git commit` that's running against an index already
    containing `D .beads/issues.jsonl` (the state `bd prime`
    leaves)."""
    hook_path = clone / ".git" / "hooks" / "pre-commit"
    hook_path.write_text(_BD_PRE_COMMIT_HOOK_SHIM)
    hook_path.chmod(0o755)


@needs_bd
def test_commit_worker_changes_against_real_git_and_real_bd(
    tmp_path: Path,
) -> None:
    """Happy commit-boundary against real git + real bd.

    Walks the upstream-bd-defect reproducer setup and asserts that
    `commit_worker_changes` produces a **code-only** commit
    (swarm-bd-export-serialization): the commit's diff touches the worker's
    file and nothing bd-export-shaped.

    - the worker's task file is committed
    - the commit does NOT touch `.beads/issues.jsonl` — not added, modified,
      or deleted. Even though `bd prime` deletes it in the worktree, the
      explicit unstage (`git reset -q -- .beads/issues.jsonl`) keeps that
      change out of the commit.
    - no rogue root-level `issues.jsonl` (the hook bypass blocks bd's hook
      from creating one).

    The negative-control test below pins that a plain `git commit` (bd hook
    live) DOES produce the buggy shape — proving the hook bypass is what
    keeps the export out, not some incidental detail of the fixture."""
    bare, clone = _make_bd_init_clone(tmp_path)

    # Cut a registered worktree from main, the same way Turma's
    # WorktreeManager does.
    worktree = tmp_path / "worktree"
    _git(clone, "worktree", "add", "-b", "task/probe/x", str(worktree), "main")

    # `bd prime` deletes `.beads/issues.jsonl` in the worktree (the upstream
    # trigger). The code-only commit must NOT stage that deletion.
    subprocess.run(
        ["bd", "prime"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # Worker-side write — a non-bd change so the commit has content.
    (worktree / "STAGE.txt").write_text("stage one complete\n")

    # Run the commit boundary.
    adapter = GitAdapter()
    sha = adapter.commit_worker_changes(
        worktree,
        "[impl] integration: code-only worker commit",
    )

    # Inspect what the commit actually CHANGED (its diff), not just the tree —
    # the tree still carries base's `.beads/issues.jsonl`; the point is that
    # this commit didn't touch it.
    changed = subprocess.run(
        ["git", "show", "--pretty=", "--name-status", sha],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    # The worker file changed.
    assert any(line.endswith("STAGE.txt") for line in changed), (
        f"STAGE.txt not in commit diff {sha}; changed: {changed}"
    )
    # The commit did NOT touch any bd export file — not `.beads/issues.jsonl`
    # (the base version stays untouched) and not a rogue root `issues.jsonl`.
    assert not any("issues.jsonl" in line for line in changed), (
        f"commit {sha} touched a bd export file; changed: {changed}"
    )


def test_commit_worker_changes_drops_a_pre_staged_bd_export(
    tmp_path: Path,
) -> None:
    """Regression (real git, no bd): a `.beads/issues.jsonl` that is already
    STAGED — bd's `export.git-add=true` does this — must not land in the commit.
    A plain `git add` pathspec exclude would NOT unstage it; the explicit
    `git reset -q` does. Pins the exact bug the pathspec-only version had."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    # commit_worker_changes runs a plain `git commit` (no inline -c identity),
    # so give the repo a local identity — CI runners have none configured.
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".beads").mkdir()
    (repo / ".beads" / "issues.jsonl").write_text('{"id":"base"}\n')
    (repo / "code.py").write_text("v0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")

    # bd-style: modify AND stage the export, plus an unstaged worker change.
    (repo / ".beads" / "issues.jsonl").write_text('{"id":"DIVERGENT"}\n')
    _git(repo, "add", ".beads/issues.jsonl")   # pre-staged
    (repo / "code.py").write_text("v1\n")        # unstaged worker change

    sha = GitAdapter().commit_worker_changes(repo, "[impl] worker change")

    changed = subprocess.run(
        ["git", "show", "--pretty=", "--name-only", sha],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    assert "code.py" in changed
    assert "issues.jsonl" not in changed, (
        f"pre-staged bd export leaked into the commit; changed: {changed!r}"
    )


def test_commit_worker_changes_commits_when_export_untracked(
    tmp_path: Path,
) -> None:
    """Regression (real git, no bd): `.beads/issues.jsonl` absent from HEAD and
    the index — reachable because task commits never track it and bd only writes
    it on a bd write/export — must NOT abort the commit. The unstage step uses
    `git reset` (a clean no-op on an unknown path), not `git restore --staged`
    (which exits 1 on an unknown pathspec)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    # commit_worker_changes runs a plain `git commit` (no inline -c identity),
    # so give the repo a local identity — CI runners have none configured.
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "code.py").write_text("v0\n")   # no .beads/issues.jsonl anywhere
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")

    (repo / "code.py").write_text("v1\n")    # worker change only

    sha = GitAdapter().commit_worker_changes(repo, "[impl] worker change")

    changed = subprocess.run(
        ["git", "show", "--pretty=", "--name-only", sha],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    assert "code.py" in changed
    assert "issues.jsonl" not in changed


@needs_bd
def test_plain_commit_after_bd_prime_reproduces_upstream_bd_bug(
    tmp_path: Path,
) -> None:
    """Negative control — the SOLE test in the suite that
    asserts the upstream bd buggy shape.

    Same setup as the happy-path test above, but uses plain
    `git -C <worktree> add -A && git commit` (no hook bypass,
    no Turma-driven explicit export). Asserts the BUGGY shape:
    root-level `issues.jsonl` added AND `.beads/issues.jsonl`
    deleted from the tree.

    If this test starts FAILING, the operator's bd version is
    likely 1.0.3+ (the upstream fix landed there as
    `steveyegge/beads#3311`, "scrub git hook env and skip
    cross-worktree git-add"). See
    `openspec/changes/swarm-worker-commit-bd-ownership/tasks.md`
    Task 5 for the recorded version-sensitivity follow-up. Under
    `swarm-bd-export-serialization` the hook bypass is load-bearing for a
    NEW reason regardless of bd version: task commits are code-only, and the
    bypass is what stops bd's pre-commit hook from re-exporting and re-adding
    `.beads/issues.jsonl` into the commit. Do NOT silence this test on 1.0.3+
    — reshape it (skipif on bd version, convert to regression-on-fixed-
    version, or remove) per the spec's Task 5 follow-up guidance.

    No other test in the suite references the buggy shape; this
    is the single source of truth on what an unexpected pass
    means and how to triage it."""
    bare, clone = _make_bd_init_clone(tmp_path)

    # Install bd's pre-commit hook so plain `git commit` fires
    # it. This is what the upstream defect needs to reproduce.
    _install_bd_pre_commit_hook(clone)

    # Same registered-worktree + bd prime + non-bd write setup
    # as the happy-path test.
    worktree = tmp_path / "worktree"
    _git(clone, "worktree", "add", "-b", "task/probe/x", str(worktree), "main")
    subprocess.run(
        ["bd", "prime"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    (worktree / "STAGE.txt").write_text("stage one complete\n")

    # Plain commit path — no hook bypass, no explicit export.
    _git(worktree, "add", "-A")
    _git(
        worktree,
        "commit", "-m", "[impl] integration: plain commit reproducer",
    )
    sha = _rev_parse(worktree, "HEAD")

    # Inspect the commit's tree for the BUGGY shape.
    tree_listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", sha],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    # If upstream is still broken, both of these assertions
    # hold: the root file appears AND the canonical path is
    # gone.
    assert "issues.jsonl" in tree_listing, (
        "Expected upstream bd defect: rogue root-level "
        "`issues.jsonl` in the plain-commit tree. If this "
        "assertion fails, see the docstring — upstream may "
        "have fixed the defect. Note the hook bypass in "
        "commit_worker_changes stays load-bearing regardless (it keeps "
        "bd's auto-export out of code-only task commits). "
        f"Tree: {tree_listing}"
    )
    assert ".beads/issues.jsonl" not in tree_listing, (
        "Expected upstream bd defect: `.beads/issues.jsonl` "
        "deleted from the plain-commit tree. If this "
        "assertion fails, see the docstring — upstream may "
        "have fixed the defect. "
        f"Tree: {tree_listing}"
    )
