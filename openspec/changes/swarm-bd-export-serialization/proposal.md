## Why

The 2026-07-21 pooled/parallel dogfood (first live run of the concurrent
multi-pool dispatcher) surfaced a merge-time defect: **every task PR after the
first conflicts on `.beads/issues.jsonl`.**

Root cause (verified in source): `commit_all_with_bd_export`
(`src/turma/swarm/git.py`) deliberately commits a fresh `bd export` of
`.beads/issues.jsonl` into *every* task branch (explicit `beads.export` →
`git add -A` → commit with `core.hooksPath=/dev/null`). Each worktree branches
from the same `base_branch`, and local `main` is fast-forwarded only once per run
(`fetch_and_ff_base` at run start), never between unmerged PRs. So the moment a
run opens ≥2 task PRs off that base, each commits a **divergent** export snapshot
(task statuses differ as claims / PR-opens mutate bd between exports) and they
conflict on the second-and-later merge.

This is **not concurrency-specific.** The sequential loop hits it too — it opens
one PR per ready task via `_main_loop` / `_run_single_task`, all off the same
un-advanced base; the earlier three-backend smoke only dodged it with
`--max-tasks 1` (one PR per run, merged before the next). Concurrent dispatch
merely makes "multiple task PRs in flight off one base" the default, so the
latent defect surfaces immediately.

The committed export is not incidental: `swarm-worker-commit-bd-ownership`
introduced it as the **worker-commit propagation pathway** — how bd state reaches
`origin/main` when a task PR merges — and preserving that propagation was an
explicit constraint of that arc. So the fix must preserve propagation while
removing the per-task snapshot. The Task-0 check (below) proved bd's own
Dolt-over-git auto-sync already propagates mutations durably across clones, so
the fix **removes the per-task export and relies on bd auto-sync** — no
git-committed export needed. (Had bd-sync not been proven, the fallback was to
re-home propagation onto a serialized, orchestrator-owned main-side path; see
the design's "Rejected fallback (Option 2)".)

## What Changes

Establish the invariant: **task branches carry code changes only; the bd export
(`.beads/issues.jsonl`) is never committed on a task branch.**

- The worker-commit boundary (`commit_all_with_bd_export` → `commit_worker_changes`,
  used by the sequential loop, the reconcile-repair phase, and the concurrent
  dispatcher) **stops staging `.beads/issues.jsonl`** on task branches. Task
  commits contain the worker's file changes and nothing bd-export-shaped.
- bd state propagates to `origin/main` through **bd's own Dolt-over-git
  auto-sync** — verified push-on-mutate and durable across clones (Task 0) — so
  Turma builds **no** separate propagation path. (A serialized, orchestrator-owned
  main-side export commit was the fallback had bd-sync not been proven; see the
  design's "Rejected fallback (Option 2)".)

## Guardrails (explicit)

- **Propagation must not silently regress.** bd state a merged task implies (task
  closed, PR-labelled, etc.) must still reach `origin/main` for other clones —
  proven (Task 0) to happen via bd's auto-sync before the per-branch export is
  removed. Converting a *visible git conflict* into *invisible cross-clone state
  loss* is the failure this change must avoid.
- **Dolt remains canonical.** `.beads/issues.jsonl` is a derived export; the git
  propagation was a convenience layer over the authoritative Dolt DB.
- The `swarm-beads-state-merge-cleanliness` revert-after-mutation contract and the
  bd-state-clean preflight stay correct and unchanged (Turma adds no new git
  writer of the export). The end-of-run "unpropagated" tail-warning is removed —
  bd's auto-sync handles propagation, so there is no lag to surface.
- Sequential single-task-per-run behavior stays byte-for-byte where it already
  avoided the conflict.

## The decision gate (bd self-sync → Option 1, SELECTED)

The pre-implementation check (operator-run, needs a live bd) **proved** bd's own
Dolt-over-git remote sync propagates *mutations* durably to other clones — a
`bd note` in one clone, its export reverted and never committed, was visible in a
fresh clone — not just pull-on-init, and Turma never passes bd's `--sandbox`
(which would disable auto-sync). So the git export propagation is obsolete
redundancy: **Option 1 is selected — the per-branch export is removed and no
serialized main-side path is built.** (Had the check failed, the fallback was the
serialized main-side path; see the design's "Rejected fallback (Option 2)". The
gate was on Task 3's shape, not on removing the per-branch export.)

## Capabilities

### Modified Capabilities

- **Worker-commit boundary** (`swarm-worker-commit-bd-ownership`): task commits no
  longer carry `.beads/issues.jsonl`; propagation is bd's Dolt auto-sync, not a
  git-committed export.

## Impact

- `src/turma/swarm/git.py` — `commit_all_with_bd_export` → `commit_worker_changes`
  (code-only staging; unstage the export via `git reset`).
- `src/turma/swarm/_orchestrator.py` — the two clean-tree checks exclude the
  export (`status_is_dirty(ignore_bd_export=True)`); the obsolete end-of-run "bd
  state unpropagated" warning + its counter are removed.
- Tests: `tests/test_swarm_git.py`, `tests/test_swarm_git_integration.py`,
  `tests/test_swarm_run.py`, plus new reproducers.
- Docs: `docs/smoke-pooled-parallel.md`; the `swarm-worker-commit-bd-ownership`
  rationale is amended (its propagation-via-task-PR mechanism is superseded).

## Out of Scope

- The concurrent dispatcher itself (shipped, `swarm-parallel-multi-pool`).
- The `core.hooksPath=/dev/null` hook-bypass — orthogonal (removable once bd
  1.0.3+ is the operator baseline; tracked separately).
- Filing bd upstream issues.
