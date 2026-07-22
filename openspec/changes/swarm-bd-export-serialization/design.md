## Scope

Remove the per-task-branch bd-export snapshot that causes merge conflicts across
sibling task PRs, and re-home bd-export propagation onto a single serialized,
orchestrator-owned path. Consumes the pooled/parallel dogfood finding described
in the proposal.

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

## Decision: serialized authority path (Option 2, default)

**Invariant:** task branches contain worker file changes only. bd-export
propagation to `origin/main`, if needed, happens exactly once via a serialized,
orchestrator-owned main-side update.

- **Worker commit** stages everything **except** `.beads/issues.jsonl`. Two
  implementation variants to choose in Task 2 by which keeps the empty-commit
  guard and the all-or-nothing failure-boundary contract cleanest:
  1. Do not export into the worktree at all; `git add -A` then never touches the
     file (it is unchanged in the worktree, so nothing to stage).
  2. Keep the export (if something downstream in the worktree needs it) but
     unstage it before commit (`git reset -- .beads/issues.jsonl`, or
     `git add -A -- ':!.beads/issues.jsonl'`).
  Prefer variant 1 unless a concrete in-worktree consumer of the export is found.
- **Propagation** becomes a single main-side export+commit owned by the
  orchestrator, run under the existing shared-state authority (the mutation lock
  in the concurrent path; the single-threaded loop otherwise), at the point Turma
  already owns main's bd state. **Merge-advancement is the natural home:** a
  task's bd state becomes durable-worthy once its PR merges and the sweep closes
  it, so exporting+committing once there yields exactly one writer, on main,
  serialized — no divergent snapshots.

### Ownership-boundary note

The `swarm-worker-commit-bd-ownership` arc deliberately avoided Turma committing
in main's working tree during a run. This change introduces exactly that, as an
explicit, narrow, serialized ownership boundary — not shared state smuggled
through task PRs. The trade is intentional: "the orchestrator owns a single
main-side bd-state commit" is a cleaner ownership model than "every task PR
carries shared state." Worker branches carry code; bd-export propagation is an
orchestrator-owned serialized main update.

## Gated simplification: bd self-sync (Option 1) — RESOLVED: SELECTED

**Task 0 ran 2026-07-21 and selected Option 1.** A `bd note` mutation in clone A,
with `.beads/issues.jsonl` reverted and never committed/pushed, was **visible in a
fresh clone B** after `bd init` bootstrapped from the remote — so bd's
Dolt-over-git auto-sync is push-on-mutate and durable across clones, not just
pull-on-init. There is no explicit `bd sync`/`bd push`; propagation is automatic
(bd's `--sandbox` disables auto-sync, and Turma never passes it — the only
`--sandbox` in the tree is the Codex worker). **The git-committed export on task
branches is therefore redundant for propagation. Task 3 = delete the git export
propagation path; no serialized main-side replacement is built.** The Task 2/3
sequencing constraint relaxes accordingly (nothing to keep propagation working —
bd already does).

Original gate procedure (kept for the record):

Pre-implementation check (operator-run — needs a live bd). Prove whether bd's
Dolt-over-git remote sync pushes *mutations* durably to other clones, not just
pulls-on-init:

1. In clone A: `bd update <id> --status …` (a real mutation) **without** committing
   `.beads/issues.jsonl`; let bd's sync run.
2. In a fresh clone B of the same remote: confirm the mutation is visible after
   bd's bootstrap/sync.

If robust and bidirectional → git-based export propagation is obsolete; **Task 3
downgrades to "delete the propagation path entirely" (Option 1)**. If not proven
→ build the serialized main-side path (Option 2). Either way the per-task-branch
export is removed. Do not adopt Option 1 on the strength of `bd init`
bootstrap alone — that proves pull-on-init, not push-on-mutate.

## Preflight / revert / tail-warning interaction

- The bd-state-clean preflight refuses to start if `.beads/issues.jsonl` is dirty
  in main's working tree; `_revert_beads_export` cleans it after Turma-owned
  mutations. With a new main-side export commit, ensure: (a) the preflight still
  holds a clean baseline at run start, (b) the main-side commit is the *only*
  sanctioned git writer of that file, and (c) the tail-mutation warning still
  reflects reality (bd state local-only vs propagated).
- No regression to same-clone reentrancy: a subsequent `turma run` in the same
  clone must still find a clean, consistent baseline.

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
- Propagation test: after merge-advancement (Option 2), the serialized main-side
  path makes a merged task's bd state reach main's export exactly once; (Option 1)
  asserts no git export is produced by task commits and documents bd-sync as the
  path.
- `test_swarm_run.py` call-sequence assertions adjusted for the moved export step.

## Deferred / out of scope

- Hook-bypass (`core.hooksPath=/dev/null`) removal — bd 1.0.3+ baseline; separate.
- The concurrent dispatcher (shipped).
- bd upstream filing.
