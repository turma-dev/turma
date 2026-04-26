"""Real-git integration tests for `GitAdapter.fetch_and_ff_base`.

Shells out to the actual `git` binary against tmpdir bare remote +
working clone fixtures. The subprocess-mock tests in
`test_swarm_git.py` validate the adapter's claimed contract against
itself; these tests validate git's actual behavior — exactly the
gap that let the prior colon-form contract ship past code review
before the 2026-04-26 live smoke caught it (the colon-form is
refused by git when the destination ref is the checked-out
branch, which is the standard `turma run` setup).

See `openspec/changes/swarm-fetch-and-ff-base-correction/` for the
contract this module backs with real-git evidence.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from turma.errors import PlanningError
from turma.swarm.git import GitAdapter


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
