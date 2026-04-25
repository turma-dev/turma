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

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

from turma.errors import PlanningError


@dataclass(frozen=True)
class PrState:
    """Single-PR state snapshot returned by `get_pr_state_by_number`.

    Used by the merge-advancement sweep to dispatch on the PR's
    current GitHub state. `state` is one of the three values
    `gh pr view --json state` returns: `OPEN` / `MERGED` /
    `CLOSED`. Draft PRs return `OPEN` from `--json state`
    (draftness lives on a separate `isDraft` field that v1 does
    not query); they fall through the OPEN branch unchanged.

    See `openspec/changes/swarm-post-merge-advancement/design.md`
    for the dispatch contract.
    """

    number: int
    state: str
    url: str


@dataclass(frozen=True)
class PrSummary:
    """Summary row returned by `list_prs_for_feature` for `turma status`.

    Carries what the status readout renders in its "pull requests"
    section: PR number, URL, GitHub state (OPEN / MERGED / CLOSED /
    DRAFT — preserved as-is from `gh`'s output so operators see the
    canonical vocabulary), title, and head branch. All strings land
    in the readout as-received from `gh pr list --json`.
    """

    number: int
    url: str
    state: str
    title: str
    head_branch: str


class _BranchNamer(Protocol):
    """Minimal slice of `WorktreeManager` that `list_prs_for_feature`
    needs. Kept as a protocol so tests can pass a tiny stub rather
    than constructing a real worktree manager."""

    def branch_name_for(self, feature: str, task_id: str) -> str: ...


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

    def find_open_pr_url_for_branch(self, branch: str) -> str | None:
        """Return the URL of an open PR whose head is `branch`, or None.

        Used by the swarm reconciliation module to distinguish
        `completion-pending` from `completion-pending-with-pr` when a
        prior `turma run` was interrupted between `gh pr create` and
        `bd close`. Uses `gh pr list` (rather than `gh pr view`) so
        "no matching PR" is a clean empty-array signal instead of a
        non-zero exit.
        """
        argv = [
            "gh", "pr", "list",
            "--head", branch,
            "--state", "open",
            "--json", "url",
        ]
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or "unknown error"
            )
            raise PlanningError(
                f"gh pr list failed: exit {result.returncode}\n{detail}"
            )
        payload = result.stdout.strip() or "[]"
        try:
            records = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PlanningError(
                "gh pr list returned non-JSON output: "
                f"{exc}\n{payload!r}"
            ) from exc
        if not isinstance(records, list):
            raise PlanningError(
                "gh pr list returned non-array JSON: "
                f"{type(records).__name__}"
            )
        if not records:
            return None
        first = records[0]
        if not isinstance(first, dict):
            return None
        url = str(first.get("url", ""))
        return url or None

    def list_prs_for_feature(
        self, feature: str, worktree_manager: _BranchNamer
    ) -> tuple[PrSummary, ...]:
        """Return one `PrSummary` per PR whose head branch matches
        `task/<feature>/*`, across every state (open / merged /
        closed / draft).

        Used by `turma status` to populate its pull-requests section
        in a single subprocess call rather than one `gh pr list` per
        task branch. `worktree_manager` is passed so the head-prefix
        is derived from the repo's own branch-name convention
        (`WorktreeManager.branch_name_for(feature, "")` →
        `task/<feature>/`) instead of re-hardcoding it in this
        module.

        argv pinned (verified against gh 2.91.0 in the
        turma-status-pr-feature-list branch):

            gh pr list
                --search head:task/<feature>/
                --state all
                --json number,url,state,title,headRefName
                --limit 100

        Returned tuple is whatever `gh` reports in its JSON order —
        gh does not guarantee a stable ordering across calls, so
        callers that need deterministic output should sort
        post-hoc.
        """
        prefix = worktree_manager.branch_name_for(feature, "")
        argv = [
            "gh", "pr", "list",
            "--search", f"head:{prefix}",
            "--state", "all",
            "--json", "number,url,state,title,headRefName",
            "--limit", "100",
        ]
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or "unknown error"
            )
            raise PlanningError(
                f"gh pr list failed: exit {result.returncode}\n{detail}"
            )
        payload = result.stdout.strip() or "[]"
        try:
            records = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PlanningError(
                "gh pr list returned non-JSON output: "
                f"{exc}\n{payload!r}"
            ) from exc
        if not isinstance(records, list):
            raise PlanningError(
                "gh pr list returned non-array JSON: "
                f"{type(records).__name__}"
            )
        return tuple(
            PrSummary(
                number=int(rec.get("number", 0)),
                url=str(rec.get("url", "")),
                state=str(rec.get("state", "")),
                title=str(rec.get("title", "")),
                head_branch=str(rec.get("headRefName", "")),
            )
            for rec in records
            if isinstance(rec, dict)
        )

    def get_pr_state_by_number(self, pr_number: int) -> PrState:
        """Look up a PR by number and return its current state.

        Used by the merge-advancement sweep
        (`openspec/changes/swarm-post-merge-advancement/`) to
        dispatch on what to do for each in-progress task carrying
        a `turma-pr:<N>` label. The sweep records the number when
        the PR is opened (via `BeadsAdapter.mark_pr_open`); this
        method is the read-side that maps `<N>` back to current
        state.

        argv pinned: `gh pr view <N> --json number,state,url`.
        Verified against gh 2.91.0 in the
        turma-status-pr-feature-list-related branch and re-
        verified against the `khanhgithead/turma-run-smoke`
        scratch.

        State vocabulary returned by `--json state`: `OPEN` /
        `MERGED` / `CLOSED`. Draft PRs return `OPEN` from this
        view; v1 does not query `isDraft` and treats drafts
        identically to non-draft open PRs.

        404 case (recorded number does not exist) is recognized
        by the canonical `gh` GraphQL phrase
        "Could not resolve to a PullRequest" in stderr and
        surfaces as a typed `PlanningError` naming the missing
        number and pointing at `bd show <task_id>` for triage —
        the merge-advancement sweep cannot guess on a missing
        PR. Other non-zero exits (auth failure, network error)
        surface stderr verbatim, same shape the rest of the
        adapter uses.
        """
        argv = [
            "gh", "pr", "view", str(pr_number),
            "--json", "number,state,url",
        ]
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "Could not resolve to a PullRequest" in stderr:
                raise PlanningError(
                    f"PR #{pr_number} not found via gh; the "
                    f"`turma-pr:{pr_number}` label is stale. Triage "
                    "with `bd show <task_id>` and "
                    "`gh pr list --search 'head:task/<feature>/'`."
                )
            detail = stderr or result.stdout.strip() or "unknown error"
            raise PlanningError(
                f"gh pr view failed: exit {result.returncode}\n{detail}"
            )
        payload = result.stdout.strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PlanningError(
                "gh pr view returned non-JSON output: "
                f"{exc}\n{payload!r}"
            ) from exc
        if not isinstance(data, dict):
            raise PlanningError(
                "gh pr view returned non-dict JSON: "
                f"{type(data).__name__}"
            )
        return PrState(
            number=int(data.get("number", pr_number)),
            state=str(data.get("state", "")),
            url=str(data.get("url", "")),
        )

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
