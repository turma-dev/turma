"""Beads CLI subprocess adapter for transcription.

Thin wrapper around the `bd` binary (Beads; https://github.com/steveyegge/beads)
that mirrors the authoring-backend pattern. Validates `shutil.which("bd")`
at construction time; delegates each operation to a single
`subprocess.run` call and raises `PlanningError` on non-zero exit with
`bd`'s stderr preserved.

Beads semantics captured here:

- Body-writing mechanism: inline `-d` / `--description <text>` flag.
  `bd create` accepts multi-line descriptions directly on the argv
  (via subprocess list form, no shell escaping). No title-prefix
  fallback is needed.
- Feature association: recorded via `--labels feature:<name>`. The
  `list_feature_tasks(feature)` method filters on that label.
- Dependency direction: `bd create --deps` uses *inverted* semantics
  (`blocks:<id>` means the new task blocks `<id>`). For the common
  "new task is blocked by <blocker>" case Turma needs, the adapter
  creates the task first and then runs `bd dep add <new> <blocker>`
  once per blocker. `bd dep add <blocked> <blocker>` means `blocked`
  depends on `blocker`, which is the direction Turma's pipeline
  produces.
- Types: bd's first-class types are `bug|feature|task|epic|chore|
  decision`. Turma's parser emits `impl|test|docs|spec`; translation
  from parser-type to bd-type lives in the pipeline (Task 3), not in
  this adapter.
- Priority: bd's scale is 0-4 (0=highest). The adapter receives
  bd-native priority; any translation from a section-number scale is
  the pipeline's job.

Swarm-orchestration semantics (added for `turma run`):

- Ready detection: `bd ready` is a first-class subcommand that uses
  Beads' GetReadyWork API (open + no active blockers + not
  in_progress / blocked / deferred). `list_ready_tasks` filters
  further client-side to exclude any task carrying the
  `needs_human_review` label, so retry-exhausted tasks stop showing
  up as ready.
- Atomic claim: `bd update <id> --claim` sets assignee and
  status=in_progress in one call, idempotent if already claimed by
  the same actor.
- Retry state: `turma-retries:<n>` label tracks attempt count,
  `needs_human_review` marks exhausted tasks, `bd note` captures the
  failure reason. `fail_task` uses a single `bd update` invocation
  to append the note, swap labels, and release status back to open
  — whatever atomicity `bd update` provides covers the whole
  transition.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from turma.errors import PlanningError


BEADS_INSTALL_HINT = (
    "bd CLI not found. Install it with `brew install beads` "
    "(Beads is a Go binary; see https://github.com/steveyegge/beads "
    "for non-macOS install paths)."
)

# bd's first-class issue types. Keeping this set here pins the argv
# contract so a bd CLI change that renames a type surfaces as an
# obvious adapter-level failure.
VALID_BD_TYPES = frozenset({
    "bug",
    "feature",
    "task",
    "epic",
    "chore",
    "decision",
})

# Label conventions used by the swarm orchestrator. Kept at module
# scope so tests can pin them.
RETRIES_LABEL_PREFIX = "turma-retries:"
NEEDS_HUMAN_REVIEW_LABEL = "needs_human_review"


def _parse_retries_from_labels(labels) -> int:
    for label in labels:
        if not isinstance(label, str):
            continue
        if not label.startswith(RETRIES_LABEL_PREFIX):
            continue
        raw = label[len(RETRIES_LABEL_PREFIX):]
        try:
            return int(raw)
        except ValueError:
            continue
    return 0


@dataclass(frozen=True)
class BeadsTaskRef:
    """Minimal record of a Beads task surfaced by `list_feature_tasks`."""

    id: str
    title: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class BeadsTaskSnapshot:
    """Beads task ref plus its current status.

    Returned by `list_feature_tasks_all_statuses`, which lists rows
    across every status. Other listers (`list_feature_tasks`,
    `list_in_progress_tasks`, `list_ready_tasks`) return
    `BeadsTaskRef` because their query already constrains the
    status — callers know `ready_tasks` are ready, so repeating
    the status on each ref there would be redundant.

    `status` is one of bd's standard values
    (`open | in_progress | blocked | deferred | closed`), or an
    empty string if the payload omitted the field entirely (which
    bd 1.0.2 does not do in practice but is tolerated here for
    robustness).
    """

    id: str
    title: str
    labels: tuple[str, ...]
    status: str


class BeadsAdapter:
    """Subprocess wrapper for the `bd` CLI."""

    def __init__(self) -> None:
        if shutil.which("bd") is None:
            raise PlanningError(BEADS_INSTALL_HINT)

    def create_task(
        self,
        *,
        title: str,
        description: str,
        bd_type: str,
        priority: int,
        feature: str,
        extra_labels: tuple[str, ...] = (),
        blocker_ids: tuple[str, ...] = (),
    ) -> str:
        """Create a new Beads task and return its id.

        Feature association is recorded via a `feature:<name>` label.
        Each `blocker_id` becomes a blocking dependency on the new task
        via a follow-up `bd dep add` call (see the module docstring for
        why `--deps` on `create` is not used).
        """
        if bd_type not in VALID_BD_TYPES:
            raise PlanningError(
                f"unsupported bd task type: {bd_type!r}. "
                f"Valid: {sorted(VALID_BD_TYPES)}"
            )
        if not 0 <= priority <= 4:
            raise PlanningError(
                f"priority out of range: {priority} (expected 0-4)"
            )

        labels = (f"feature:{feature}", *extra_labels)
        argv = [
            "bd", "create",
            "--silent",
            "--type", bd_type,
            "--priority", str(priority),
            "--description", description,
            "--labels", ",".join(labels),
            title,
        ]
        result = self._run(argv, step="bd create")
        new_id = result.stdout.strip()
        if not new_id:
            raise PlanningError(
                "bd create returned empty stdout; "
                f"stderr: {result.stderr!r}"
            )

        for blocker in blocker_ids:
            self._add_dependency(blocked_id=new_id, blocker_id=blocker)

        return new_id

    def close_task(self, task_id: str) -> None:
        """Close a Beads task by id."""
        self._run(["bd", "close", task_id], step="bd close")

    def list_feature_tasks(
        self, feature: str
    ) -> tuple[BeadsTaskRef, ...]:
        """List open tasks tagged with `feature:<name>`."""
        argv = [
            "bd", "list",
            "--label", f"feature:{feature}",
            "--json",
            "--limit", "0",
        ]
        result = self._run(argv, step="bd list")
        payload = result.stdout.strip()
        if not payload:
            return ()
        try:
            records = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PlanningError(
                f"bd list returned non-JSON output: {exc}\n{payload!r}"
            ) from exc
        if not isinstance(records, list):
            raise PlanningError(
                f"bd list returned non-array JSON: {type(records).__name__}"
            )
        return tuple(
            BeadsTaskRef(
                id=str(rec["id"]),
                title=str(rec.get("title", "")),
                labels=tuple(str(label) for label in rec.get("labels", ())),
            )
            for rec in records
            if isinstance(rec, dict) and "id" in rec
        )

    def list_feature_tasks_all_statuses(
        self, feature: str
    ) -> tuple[BeadsTaskSnapshot, ...]:
        """List every feature-tagged task regardless of status.

        Unlike `list_feature_tasks` (which defaults to `open` per
        bd's `list` semantics), this method returns closed and
        any-other-status rows too so `turma status` can populate a
        complete counter block (ready / in_progress /
        blocked-or-deferred / closed / needs_human_review) and
        surface cleanup residues — e.g. a branch left on disk for
        a task whose `close_task` landed but whose
        `WorktreeManager.cleanup` did not complete.

        Returns `BeadsTaskSnapshot` rather than `BeadsTaskRef`
        because the mixed-status payload requires status
        disambiguation on each row; `turma status`'s counter block
        buckets tasks by `snapshot.status`, which the narrower
        `BeadsTaskRef` does not carry.

        argv pinned: `bd list --all --label feature:<name> --json
        --limit 0`. Verified against bd 1.0.2 in the
        turma-status-beads-all-statuses branch: `--all` documents
        as "Show all issues including closed (overrides default
        filter)" and was observed to return the same set as the
        explicit status enumeration `--status
        open,in_progress,blocked,deferred,closed` on the live
        smoke scratch. `--all` is preferred for forward-compat
        with any future bd status vocabulary evolution.
        """
        argv = [
            "bd", "list",
            "--all",
            "--label", f"feature:{feature}",
            "--json",
            "--limit", "0",
        ]
        result = self._run(argv, step="bd list (all statuses)")
        payload = result.stdout.strip()
        if not payload:
            return ()
        try:
            records = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PlanningError(
                "bd list (all statuses) returned non-JSON output: "
                f"{exc}\n{payload!r}"
            ) from exc
        if not isinstance(records, list):
            raise PlanningError(
                "bd list (all statuses) returned non-array JSON: "
                f"{type(records).__name__}"
            )
        return tuple(
            BeadsTaskSnapshot(
                id=str(rec["id"]),
                title=str(rec.get("title", "")),
                labels=tuple(str(label) for label in rec.get("labels", ())),
                status=str(rec.get("status", "")),
            )
            for rec in records
            if isinstance(rec, dict) and "id" in rec
        )

    def list_in_progress_tasks(
        self, feature: str
    ) -> tuple[BeadsTaskRef, ...]:
        """List IN_PROGRESS + feature-tagged tasks.

        Used by the swarm reconciliation module at run-start to walk
        tasks Beads believes are already claimed — the candidate set
        for "prior run left state behind" classification.
        """
        argv = [
            "bd", "list",
            "--status", "in_progress",
            "--label", f"feature:{feature}",
            "--json",
            "--limit", "0",
        ]
        result = self._run(argv, step="bd list (in_progress)")
        payload = result.stdout.strip()
        if not payload:
            return ()
        try:
            records = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PlanningError(
                "bd list (in_progress) returned non-JSON output: "
                f"{exc}\n{payload!r}"
            ) from exc
        if not isinstance(records, list):
            raise PlanningError(
                "bd list (in_progress) returned non-array JSON: "
                f"{type(records).__name__}"
            )
        return tuple(
            BeadsTaskRef(
                id=str(rec["id"]),
                title=str(rec.get("title", "")),
                labels=tuple(str(label) for label in rec.get("labels", ())),
            )
            for rec in records
            if isinstance(rec, dict) and "id" in rec
        )

    def list_ready_tasks(
        self, feature: str
    ) -> tuple[BeadsTaskRef, ...]:
        """List OPEN + feature-tagged + unblocked tasks that are claimable.

        Uses `bd ready` (Beads' GetReadyWork API — open, not
        in_progress / blocked / deferred, with all blockers satisfied)
        and filters client-side to exclude tasks carrying the
        `needs_human_review` label so retry-exhausted tasks stop
        appearing as ready work.
        """
        argv = [
            "bd", "ready",
            "--label", f"feature:{feature}",
            "--json",
            "--limit", "0",
        ]
        result = self._run(argv, step="bd ready")
        payload = result.stdout.strip()
        if not payload:
            return ()
        try:
            records = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PlanningError(
                f"bd ready returned non-JSON output: {exc}\n{payload!r}"
            ) from exc
        if not isinstance(records, list):
            raise PlanningError(
                f"bd ready returned non-array JSON: "
                f"{type(records).__name__}"
            )
        return tuple(
            BeadsTaskRef(
                id=str(rec["id"]),
                title=str(rec.get("title", "")),
                labels=tuple(str(label) for label in rec.get("labels", ())),
            )
            for rec in records
            if isinstance(rec, dict)
            and "id" in rec
            and NEEDS_HUMAN_REVIEW_LABEL
            not in [str(label) for label in rec.get("labels", ())]
        )

    def claim_task(self, task_id: str) -> None:
        """Atomically transition an open task to in_progress.

        Uses `bd update <id> --claim`, which bd documents as atomic
        (assignee + status=in_progress in one call, idempotent if the
        task is already claimed by the same actor). Non-zero exit —
        typically a claim race with another actor — raises
        `PlanningError` with `bd` stderr preserved.
        """
        self._run(
            ["bd", "update", task_id, "--claim"],
            step="bd update --claim",
        )

    def get_task_body(self, task_id: str) -> str:
        """Return the description body of a Beads task.

        The swarm orchestrator passes this through to the worker as
        `WorkerInvocation.description` — the authoritative subtask
        list the worker executes. Missing / empty descriptions surface
        as an empty string; callers decide whether that is an error.
        """
        result = self._run(
            ["bd", "show", task_id, "--json"],
            step="bd show (body)",
        )
        payload = result.stdout.strip()
        if not payload:
            return ""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PlanningError(
                "bd show returned non-JSON output: "
                f"{exc}\n{payload!r}"
            ) from exc
        if isinstance(data, list):
            if not data:
                return ""
            data = data[0]
        if not isinstance(data, dict):
            raise PlanningError(
                "bd show returned unexpected JSON shape: "
                f"{type(data).__name__}"
            )
        for key in ("description", "body"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        return ""

    def retries_so_far(self, task_id: str) -> int:
        """Return the integer encoded in a `turma-retries:<n>` label.

        Returns 0 if no such label is present on the task. Used by the
        orchestrator to compute the retry budget before calling
        `fail_task`.
        """
        result = self._run(
            ["bd", "show", task_id, "--json"],
            step="bd show",
        )
        payload = result.stdout.strip()
        if not payload:
            return 0
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PlanningError(
                f"bd show returned non-JSON output: {exc}\n{payload!r}"
            ) from exc
        if isinstance(data, list):
            if not data:
                return 0
            data = data[0]
        if not isinstance(data, dict):
            raise PlanningError(
                "bd show returned unexpected JSON shape: "
                f"{type(data).__name__}"
            )
        return _parse_retries_from_labels(data.get("labels") or [])

    def fail_task(
        self,
        task_id: str,
        reason: str,
        *,
        retries_so_far: int,
        max_retries: int,
    ) -> None:
        """Record a worker failure against a task.

        Appends the reason as a note, swaps the `turma-retries:<n>`
        label to reflect the new attempt count (or adds
        `needs_human_review` on exhaustion), and releases the claim
        back to `open` — all in a single `bd update` invocation so
        whatever atomicity bd provides covers the whole transition.

        NOT idempotent across its internal steps in the sense that a
        partial-failure inside bd itself would require operator
        triage via `bd show <task_id>`. The adapter surfaces the
        failed step's argv and stderr in the raised PlanningError so
        triage is concrete.
        """
        argv = ["bd", "update", task_id, "--append-notes", reason]

        if retries_so_far > 0:
            argv += [
                "--remove-label",
                f"{RETRIES_LABEL_PREFIX}{retries_so_far}",
            ]

        new_retries = retries_so_far + 1
        if new_retries > max_retries:
            argv += ["--add-label", NEEDS_HUMAN_REVIEW_LABEL]
        else:
            argv += [
                "--add-label",
                f"{RETRIES_LABEL_PREFIX}{new_retries}",
            ]

        argv += ["--status", "open"]

        self._run(argv, step="bd update (fail_task)")

    def _add_dependency(self, *, blocked_id: str, blocker_id: str) -> None:
        # `bd dep add <blocked> <blocker>` — blocked depends on blocker.
        self._run(
            ["bd", "dep", "add", blocked_id, blocker_id],
            step="bd dep add",
        )

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
