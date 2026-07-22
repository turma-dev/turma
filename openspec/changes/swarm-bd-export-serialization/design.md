## Scope

Remove the per-task-branch bd-export snapshot that causes merge conflicts across
sibling task PRs, and rely on bd's own verified Dolt auto-sync for propagation
(Option 1, selected) — Turma builds no propagation path of its own. A serialized,
orchestrator-owned main-side path was the rejected fallback (see below). Consumes
the pooled/parallel dogfood finding described in the proposal.

## Current state (what we are changing)

`commit_all_with_bd_export` (`src/turma/swarm/git.py`) is the worker-commit
boundary shared by three callers:

- the sequential loop (`_run_single_task`),
- the reconcile-repair phase, and
- the concurrent dispatcher (`dispatch_concurrent`).

Per task commit it runs: `beads.export(-> <worktree>/.beads/issues.jsonl,
cwd=repo_root)` → `git add -A` → `commit -c core.hooksPath=/dev/null`. So every
task branch carries a snapshot of `.beads/issues.jsonl`. Because worktrees branch
from the same `base_branch` and local `main` is FF-advanced only once per run,
sibling PRs opened in one run carry divergent snapshots → merge conflict on the
second-and-later merge.

## Decision (Option 1): task commits are code-only; bd auto-sync propagates

**Invariant:** task branches contain worker file changes only —
`.beads/issues.jsonl` is never committed on a task branch. bd state reaches
`origin/main` through bd's own Dolt-over-git auto-sync; Turma builds **no**
separate propagation path.

This is the resolved decision (see "The decision gate" below): the Task-0 check
proved bd auto-sync is push-on-mutate and durable across clones, so committing
the export on task branches is pure redundancy. Task 3 therefore *deletes* the
git export propagation and builds no main-side replacement. The serialized
main-side path was the fallback had bd-sync not been proven — kept in "Rejected
fallback (Option 2)" below.

- **Worker commit** stages everything **except** `.beads/issues.jsonl`.
  Implemented (Task 2): stop exporting into the worktree, `git add -A`, then
  explicitly `git reset -q -- .beads/issues.jsonl` before committing. The
  un-stage — not a `git add` pathspec exclude — is load-bearing: bd's
  `export.git-add=true` default can leave the export **already staged**, and a
  pathspec exclude on `add` only controls what `add` stages; it cannot un-stage
  an already-staged entry, so the export would still be committed. `git reset`
  drops the path from the index regardless of how it got staged (modified,
  deleted, added) and — unlike `git restore --staged`, which exits 1 on an
  unknown pathspec — is a clean no-op when the export is untracked (reachable
  because task commits never track it). The empty-commit guard (also
  export-excluded) and the failure-boundary contract both tolerate it cleanly.
- **Propagation** is bd's Dolt-over-git auto-sync — on by default (Turma never
  passes `--sandbox`, which would disable it). Turma neither exports nor commits
  `.beads/issues.jsonl` during a run; the tracked export is a regenerable
  backup, not the propagation path.

## The decision gate (Task 0): resolved → Option 1

**Task 0 ran 2026-07-21 and selected Option 1.** A `bd note` mutation in clone A,
with `.beads/issues.jsonl` reverted and never committed/pushed, was **visible in a
fresh clone B** after `bd init` bootstrapped from the remote — so bd's
Dolt-over-git auto-sync is push-on-mutate and durable across clones, not just
pull-on-init. There is no explicit `bd sync`/`bd push`; propagation is automatic
(bd's `--sandbox` disables auto-sync, and Turma never passes it — the only
`--sandbox` in the tree is the Codex worker). The git-committed export on task
branches is therefore redundant for propagation.

Original gate procedure (kept for the record):

Pre-implementation check (operator-run — needs a live bd). Prove whether bd's
Dolt-over-git remote sync pushes *mutations* durably to other clones, not just
pulls-on-init:

1. In clone A: `bd update <id> --status …` (a real mutation) **without** committing
   `.beads/issues.jsonl`; let bd's sync run.
2. In a fresh clone B of the same remote: confirm the mutation is visible after
   bd's bootstrap/sync.

If robust and bidirectional → git-based export propagation is obsolete (Option 1,
selected). If not proven → the serialized main-side path (Option 2, below).
Either way the per-task-branch export is removed. Do not adopt Option 1 on the
strength of `bd init` bootstrap alone — that proves pull-on-init, not
push-on-mutate.

## Rejected fallback (Option 2): serialized main-side propagation

**Not built** — Task 0 proved bd's own sync propagates, making this unnecessary.
Recorded for the reasoning, and in case a future bd change ever weakens
auto-sync.

Had the check failed, propagation would move to a single main-side export+commit
owned by the orchestrator, run under the existing shared-state authority (the
mutation lock in the concurrent path; the single-threaded loop otherwise), at the
point Turma already owns main's bd state — merge-advancement being the natural
home (a task's bd state becomes durable-worthy once its PR merges and the sweep
closes it), yielding exactly one writer, on main, serialized, with no divergent
snapshots. That path would introduce Turma committing in main's working tree
during a run — which `swarm-worker-commit-bd-ownership` deliberately avoided; the
trade ("the orchestrator owns a single main-side bd-state commit" vs "every task
PR carries shared state") would have been intentional. Option 1 sidesteps it
entirely by letting bd own propagation.

## Preflight / revert interaction

- The bd-state-clean preflight refuses to start if `.beads/issues.jsonl` is dirty
  in main's working tree; `_revert_beads_export` cleans it after Turma-owned
  mutations. **Unchanged by Option 1:** Turma adds no new git writer of the
  export, so the clean-baseline invariant and same-clone reentrancy hold exactly
  as before. The end-of-run "bd state unpropagated" warning is removed — bd's
  auto-sync handles propagation, so there is no lag to surface.

## Tests

- **Reproducer (red first).** A single run over **two independent ready tasks**
  produces two PR-branch commits whose trees **do not contain / do not modify**
  `.beads/issues.jsonl`. Fails today (both branches carry divergent snapshots);
  passes once task commits are code-only. Concurrency-free on purpose — it pins
  the true cause (multiple PRs off one base), not a parallel artifact.
- Worker-commit boundary tests (`test_swarm_git.py`,
  `test_swarm_git_integration.py`): the committed tree no longer contains
  `.beads/issues.jsonl`; the empty-commit guard and failure-boundary contract are
  preserved.
- No git export is produced by task commits — the committed tree/diff never
  touches `.beads/issues.jsonl`, verified against real git for both the
  pre-staged and the untracked cases; bd's Dolt auto-sync is the propagation
  path (Task 0). No main-side propagation commit exists to test.
- `test_swarm_run.py` call-sequence assertions adjusted for the moved export step.

## Deferred / out of scope

- Hook-bypass (`core.hooksPath=/dev/null`) removal — bd 1.0.3+ baseline; separate.
- The concurrent dispatcher (shipped).
- bd upstream filing.
