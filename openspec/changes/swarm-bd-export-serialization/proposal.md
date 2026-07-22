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
explicit constraint of that arc. So the fix cannot simply drop the export; it
must re-home propagation onto a path that does not put a shared, divergent
snapshot on every task branch.

## What Changes

Establish the invariant: **task branches carry code changes only; bd-export
propagation, if needed, happens exactly once through a serialized,
orchestrator-owned authority path — never smuggled through per-task PRs.**

- The worker-commit boundary (`commit_all_with_bd_export`, used by the sequential
  loop, the reconcile-repair phase, and the concurrent dispatcher) **stops
  staging `.beads/issues.jsonl`** on task branches. Task commits contain the
  worker's file changes and nothing bd-export-shaped.
- bd-export propagation to `origin/main` moves to a single **serialized,
  main-side** update owned by the orchestrator (default: at merge-advancement /
  under the authority that already owns main's bd state), so exactly one
  authoritative export reaches origin, with no divergent branch snapshots to
  conflict.

## Guardrails (explicit)

- **Propagation must not silently regress.** bd state a merged task implies (task
  closed, PR-labelled, etc.) must still reach `origin/main` for other clones —
  via the new serialized path, verified by test, before the per-branch export is
  removed. Converting a *visible git conflict* into *invisible cross-clone state
  loss* is the failure this change must avoid.
- **Dolt remains canonical.** `.beads/issues.jsonl` is a derived export; the git
  propagation is a convenience layer over the authoritative Dolt DB.
- The `swarm-beads-state-merge-cleanliness` revert-after-mutation contract, the
  bd-state-clean preflight, and the tail-mutation warning stay correct — adjusted
  only as the new main-side update requires.
- Sequential single-task-per-run behavior stays byte-for-byte where it already
  avoided the conflict.

## Gated simplification (bd self-sync → Option 1)

If a pre-implementation check proves bd's own Dolt-over-git remote sync
propagates *mutations* durably and bidirectionally to other clones — the dogfood
showed `bd init` bootstrapping from the remote, but that only proves
pull-on-init, not push-on-mutate — then the git export propagation is obsolete
redundancy and the serialized main-side update collapses to "remove it entirely."
Until that is proven, the serialized main-side path is built (the safe default:
relying on unverified bd behavior risks turning a visible conflict into invisible
cross-clone state loss). The check is a gate on Task 3's shape; it does not block
authoring this change or removing the per-branch export.

## Capabilities

### Modified Capabilities

- **Worker-commit boundary** (`swarm-worker-commit-bd-ownership`): task commits no
  longer carry `.beads/issues.jsonl`; export/propagation responsibility moves to a
  serialized, orchestrator-owned step.

## Impact

- `src/turma/swarm/git.py` — `commit_all_with_bd_export` staging/export behavior.
- `src/turma/swarm/_orchestrator.py` — serialized main-side propagation; preflight
  / revert / tail-warning coherence.
- Tests: `tests/test_swarm_git.py`, `tests/test_swarm_git_integration.py`,
  `tests/test_swarm_run.py`, plus a new failing reproducer.
- Docs: `docs/smoke-pooled-parallel.md`; the `swarm-worker-commit-bd-ownership`
  rationale is amended (its propagation-via-task-PR mechanism is superseded).

## Out of Scope

- The concurrent dispatcher itself (shipped, `swarm-parallel-multi-pool`).
- The `core.hooksPath=/dev/null` hook-bypass — orthogonal (removable once bd
  1.0.3+ is the operator baseline; tracked separately).
- Filing bd upstream issues.
