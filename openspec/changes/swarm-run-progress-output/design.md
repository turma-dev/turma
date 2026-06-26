## Scope

One function, four print lines. This change adds per-task lifecycle
progress output to the success path of `_run_single_task` in
`src/turma/swarm/_orchestrator.py`. It is deliberately the smallest cut
that gives the operator visibility during the long silent worker wait,
without opening a general observability project.

No control flow changes. No adapter changes. No state machine changes.
The lines are additive stdout only; removing them would leave behavior
identical.

## Exact lines and placement

`_run_single_task(feature, task, services)` today, with the new lines
marked `+`:

```
ref = services.worktree.setup(feature=..., task_id=task.id, ...)
+ print(f"worktree: setup {task.id}")
_clear_sentinels(ref.path)
description = services.beads.get_task_body(task.id)
invocation = WorkerInvocation(..., timeout_seconds=services.worker_timeout)
worker = services.worker_factory()
+ print(f"worker: running {task.id} (timeout {services.worker_timeout}s)")
result = worker.run(invocation)

if result.status != "success":
    return _handle_failure(...)          # unchanged — speaks for itself
if not services.git.status_is_dirty(ref.path):
    return _handle_failure(...)          # unchanged

try:
    message = _render_commit_message(task, feature)
    services.git.commit_all_with_bd_export(ref.path, message, ...)
+   print(f"commit: {task.id}")
    services.git.push_branch(ref.path, ref.branch)
+   print(f"push: {task.id}")
except PlanningError as exc:
    return _handle_failure(...)          # unchanged

# ... open_pr, mark_pr_open, existing final line unchanged:
print(f"swarm: opened {task.id} (PR: {pr_url}; awaiting merge)")
```

### Placement rationale

- `worktree: setup` prints **after** `setup` returns — it confirms a
  completed step, matching the post-action style of `fetch:` and
  `swarm: claimed`.
- `worker: running` prints **before** `worker.run`. This is the one
  pre-action line, and intentionally so: the whole point is to name the
  long wait *before* the process blocks. It includes the timeout so the
  operator knows the upper bound on the silence.
- `commit:` prints after `commit_all_with_bd_export` succeeds and before
  `push_branch`; `push:` prints after `push_branch` succeeds. Placing
  them inside the `try` after each call means a failure in either step
  falls through to `_handle_failure` without a misleading success line.

## Determinism

The only interpolated values are `task.id` (stable) and
`services.worker_timeout` (config-fixed for the run). No timestamps,
durations, PIDs, or other run-varying content. The same run produces the
same lines in the same order, which is what makes them `capsys`-pinnable
as exact-string assertions.

## Failure path: deliberately silent on lifecycle

The three failure exits (`worker.run` non-success, clean tree, commit/
push `PlanningError`) are unchanged. `_handle_failure` already prints:

```
swarm: <id> failed (attempt N/M): <reason>
swarm: <id> failed (budget exhausted after N attempts): <reason>
```

So a failed task still produces output; it just doesn't get
`worker: complete` / `worker: failed` lifecycle bookends. Adding those
would require touching the failure-path tests for marginal value and is
explicitly out of scope (see proposal).

The ordering guarantee this creates: on a failed worker run the operator
sees `worker: running <id> ...` followed by `swarm: <id> failed ...`,
with **no** `commit:` / `push:` in between. The tests pin that negative.

## Interaction with the existing output contract

This sits under the "compact per-task summary as the loop progresses"
contract (`swarm-orchestration/design.md`, `tasks.md:253-255`). The new
lines share that prefix-`:` compact style and are additive — no existing
assertion's matched substring changes.

Checked against the existing `not in` assertions in
`tests/test_swarm_run.py` (`fetch: origin/main → main` @554,
`orphan branch (operator triage)` @1752, and the merge-advancement
negatives), none of which overlap the new `worktree:` / `worker:` /
`commit:` / `push:` prefixes. New lines will not trip them.

## Tests

`tests/test_swarm_run.py`, red-green per acceptance line:

1. **Happy-path success test** (the existing test that drives
   `_run_single_task` to `open_pr` with stub services): extend its
   `capsys.readouterr()` assertions to require, in `captured.out`:
   - `worktree: setup <stub task id>`
   - `worker: running <stub task id> (timeout <stub worker_timeout>s)`
   - `commit: <stub task id>`
   - `push: <stub task id>`
   Pin the `worker: running` line against the stub's configured
   `worker_timeout` so the `(timeout Ns)` value is exact, not fuzzy.
2. **Worker-failure path test** (existing failing-worker test): assert
   `worker: running <id>` **is** present (the wait was announced) and
   that `commit: <id>` and `push: <id>` are **absent** (no false
   success markers before `swarm: <id> failed`).
3. No new test module; both assertions attach to existing tests to keep
   the change a focused additive diff.

## Out of items deferred past this change

- Completion bookends (`worker: complete` / `worker: failed`).
- Any preflight / fetch / dry-run phase-walk framing.
- Durations / timestamps / streamed worker output / JSON mode.

These are listed so a future observability change has a clear starting
boundary; none are implied or half-built here.
