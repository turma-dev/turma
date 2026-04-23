"""Per-task git worktree manager.

Owns the worktree-lifecycle operations the swarm orchestrator needs
between claim and worker-run, and between close_task and the next
ready-task lookup:

- `setup(*, feature, task_id, base_branch)` creates (or reuses) a
  worktree at `<worktree_root>/<feature>/<task_id>/` on branch
  `task/<feature>/<task_id>` and returns a frozen `WorktreeRef`.
- `cleanup(ref)` removes the worktree and deletes its branch. The
  orchestrator calls this only after a task reaches `closed` in
  Beads — on failure paths the worktree is left in place as the
  primary triage artifact (see `openspec/changes/swarm-
  orchestration/design.md` "Worktree contract").

argv pinned by unit tests so upstream git changes surface as failing
tests:

- `git -C <repo_root> worktree list --porcelain`
- `git -C <repo_root> worktree add <path> -b <branch> <base>`
- `git -C <repo_root> worktree remove --force <path>`
- `git -C <repo_root> branch -D <branch>`
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from turma.errors import PlanningError


GIT_INSTALL_HINT = (
    "git CLI not found. Install it (e.g. `brew install git` on macOS) "
    "and re-run."
)


@dataclass(frozen=True)
class WorktreeRef:
    """Identity of a per-task worktree.

    Both `path` and `branch` are derivable from the feature + task_id
    the orchestrator already holds, but keeping them together in one
    frozen record lets `cleanup` take a single parameter and removes
    the temptation for callers to recompute either.
    """

    path: Path
    branch: str


class WorktreeManager:
    """git worktree operations scoped to one Turma repo root."""

    def __init__(
        self,
        repo_root: Path,
        worktree_root: str = ".worktrees",
    ) -> None:
        if shutil.which("git") is None:
            raise PlanningError(GIT_INSTALL_HINT)
        self._repo_root = Path(repo_root).resolve()
        self._worktree_root = worktree_root

    def setup(
        self,
        *,
        feature: str,
        task_id: str,
        base_branch: str,
    ) -> WorktreeRef:
        """Create or reuse the worktree for this (feature, task_id)."""
        target_path = self._worktree_path(feature, task_id).resolve()
        branch = self._branch_name(feature, task_id)

        if self._worktree_is_registered(target_path):
            return WorktreeRef(path=target_path, branch=branch)

        self._run(
            [
                "git", "-C", str(self._repo_root),
                "worktree", "add",
                str(target_path),
                "-b", branch,
                base_branch,
            ],
            step="git worktree add",
        )
        return WorktreeRef(path=target_path, branch=branch)

    def cleanup(self, ref: WorktreeRef) -> None:
        """Remove the worktree and delete its branch.

        Graceful when the branch has already been deleted (e.g. an
        earlier partial cleanup). `git worktree remove` failures
        always surface as PlanningError because a failing remove
        leaves real state on disk.
        """
        self._run(
            [
                "git", "-C", str(self._repo_root),
                "worktree", "remove", "--force",
                str(ref.path),
            ],
            step="git worktree remove",
        )
        try:
            self._run(
                [
                    "git", "-C", str(self._repo_root),
                    "branch", "-D", ref.branch,
                ],
                step="git branch -D",
            )
        except PlanningError as exc:
            # Tolerate "branch not found": the branch may have been
            # deleted between `worktree remove` and this call, or the
            # worktree may never have created the branch (shouldn't
            # happen today, but keeping cleanup graceful avoids
            # spurious failures on triage replays).
            if "not found" not in str(exc) and "does not exist" not in str(exc):
                raise

    def worktree_path_for(self, feature: str, task_id: str) -> Path:
        """Resolved path where the worktree for (feature, task_id) lives.

        Pure derivation — no git call, no filesystem touch. Exposed so
        reconciliation can answer "is the worktree directory present?"
        without round-tripping through `setup()` (which mutates).
        """
        return self._worktree_path(feature, task_id).resolve()

    def branch_name_for(self, feature: str, task_id: str) -> str:
        """Branch name this manager would use for (feature, task_id)."""
        return self._branch_name(feature, task_id)

    def list_task_branches(self, feature: str) -> tuple[str, ...]:
        """List local branches matching `task/<feature>/*`.

        Used by reconciliation to detect orphan branches — branches
        with the swarm's task naming convention that no longer
        correspond to an in_progress Beads task. Uses
        `for-each-ref` rather than `branch --list` so the output is
        clean (no `*` current-branch marker, no leading whitespace).
        """
        result = self._run(
            [
                "git", "-C", str(self._repo_root),
                "for-each-ref",
                "--format=%(refname:short)",
                f"refs/heads/task/{feature}/",
            ],
            step="git for-each-ref",
        )
        return tuple(
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        )

    def _worktree_path(self, feature: str, task_id: str) -> Path:
        root = Path(self._worktree_root)
        if not root.is_absolute():
            root = self._repo_root / root
        return root / feature / task_id

    @staticmethod
    def _branch_name(feature: str, task_id: str) -> str:
        return f"task/{feature}/{task_id}"

    def _worktree_is_registered(self, target_path: Path) -> bool:
        result = self._run(
            [
                "git", "-C", str(self._repo_root),
                "worktree", "list", "--porcelain",
            ],
            step="git worktree list",
        )
        want = str(target_path)
        for line in result.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            recorded = line[len("worktree "):].strip()
            # Normalize both sides so symlinks / trailing slashes
            # don't cause a false negative.
            if Path(recorded).resolve() == target_path:
                return True
            if recorded == want:
                return True
        return False

    @staticmethod
    def _run(
        argv: list[str],
        *,
        step: str,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or "unknown error"
            )
            raise PlanningError(
                f"{step} failed: exit {result.returncode}\n{detail}"
            )
        return result
