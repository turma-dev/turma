"""GitHub PR creation boundary between `git push` and `close_task`.

After `GitAdapter.push_branch` lands the task branch on the remote,
the orchestrator calls `PullRequestAdapter.open_pr(...)` to submit the
PR and capture the URL. The adapter is a thin subprocess wrapper
around `gh pr create`: auth is the operator's responsibility (checked
once at construction via `gh auth status`), and non-zero exits surface
`gh`'s stderr verbatim so PAT-scope / branch-protection / network
failures land in the caller's `PlanningError` unchanged.

No retry on transient GitHub failures in v1.
"""

from __future__ import annotations

import shutil
import subprocess

from turma.errors import PlanningError


GH_INSTALL_HINT = (
    "gh CLI not found. Install GitHub CLI (e.g. `brew install gh` on "
    "macOS) and run `gh auth login`."
)

GH_AUTH_HINT = (
    "gh session not authenticated. Run `gh auth login` and retry."
)


class PullRequestAdapter:
    """Thin subprocess wrapper around `gh pr create`."""

    def __init__(self) -> None:
        if shutil.which("gh") is None:
            raise PlanningError(GH_INSTALL_HINT)
        self._check_auth()

    def open_pr(
        self,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        """Open a PR from `branch` into `base` and return its URL."""
        argv = [
            "gh", "pr", "create",
            "--head", branch,
            "--base", base,
            "--title", title,
            "--body", body,
        ]
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or "unknown error"
            )
            raise PlanningError(
                f"gh pr create failed: exit {result.returncode}\n{detail}"
            )

        url = _extract_pr_url(result.stdout)
        if not url:
            raise PlanningError(
                "gh pr create returned no PR URL on stdout:\n"
                f"{result.stdout}"
            )
        return url

    @staticmethod
    def _check_auth() -> None:
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True
        )
        if result.returncode != 0:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or "unknown error"
            )
            raise PlanningError(f"{GH_AUTH_HINT}\n{detail}")


def _extract_pr_url(stdout: str) -> str:
    """Return the first GitHub PR URL in `stdout`, or empty if none.

    `gh pr create` prints the PR URL on its own line. A remote-hint
    preamble (`Creating pull request ...`) sometimes precedes it, so
    we scan all lines rather than taking the first.
    """
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("https://") and "/pull/" in line:
            return line
    return ""
