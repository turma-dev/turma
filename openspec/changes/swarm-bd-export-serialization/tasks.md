## Tasks

### 0. Pre-implementation gate — bd self-sync check (operator, separate) ✓ DONE
- [x] Ran 2026-07-21. Result: **Option 1 selected.** A `bd note` in clone A (export
  reverted, never committed) was visible in a fresh clone B after `bd init` —
  bd auto-sync is push-on-mutate + durable. Turma never passes bd `--sandbox`.
- [x] Consequence: Task 3 = delete the git export propagation path (no serialized
  main-side replacement). Task 2/3 sequencing constraint relaxes.

### 1. Failing reproducer first
- [ ] Add a test: a single `turma run` over **two independent ready tasks** yields
  two PR-branch commits that must NOT modify `.beads/issues.jsonl`. Red today.
- [ ] Keep it concurrency-free (sequential loop, two ready tasks) so it pins the
  true cause — multiple task PRs off one un-advanced base — not a parallel
  artifact.

### 2. Task branches carry code only
- [ ] Change the worker-commit boundary (`commit_all_with_bd_export`) so task
  commits exclude `.beads/issues.jsonl` (prefer: don't export into the worktree;
  fall back to export-then-unstage only if an in-worktree consumer exists). Keep
  the empty-commit guard and the all-or-nothing failure-boundary contract intact.
- [ ] Update `test_swarm_git.py` / `test_swarm_git_integration.py` to assert the
  committed tree is code-only.
- [ ] **Sequencing (Option 2):** land Task 3's serialized main-side propagation in
  the **same change**. Removing the per-branch export without the replacement path
  would regress propagation (proposal guardrail: propagation must not regress).
  Task 2 and Task 3 are only separable if Task 0 selects Option 1 (no replacement
  path needed).

### 3. Remove the git export propagation path (Option 1 — SELECTED by Task 0)
- [ ] Delete the now-redundant export/propagation intent from the worker-commit
  boundary: no `beads.export` into the worktree, no `.beads/issues.jsonl` in task
  commits. bd's Dolt auto-sync is the propagation path.
- [ ] Rename/retitle `commit_all_with_bd_export` if the name no longer fits (it no
  longer does a bd export), or keep + document. Update all three callers
  (`_run_single_task`, repair phase, `dispatch_concurrent`).
- [ ] Test that task commits produce no `.beads/issues.jsonl`; document bd's
  Dolt-sync as the propagation path in the code + rationale.
- [ ] *(Not building Option 2's serialized main-side commit — bd self-sync makes
  it unnecessary. Recorded in design.md for the record.)*

### 4. Preflight / revert / tail-warning coherence
- [ ] Ensure the bd-state-clean preflight, `_revert_beads_export`, and the
  tail-mutation warning stay correct with the new main-side writer; no
  same-clone-reentrancy regression. Add/adjust tests as needed.

### 5. Docs + rationale + baseline
- [ ] Amend `swarm-worker-commit-bd-ownership` — its propagation-via-task-PR
  mechanism is superseded (note, don't silently contradict).
- [ ] Update `docs/smoke-pooled-parallel.md`: no more `.beads/issues.jsonl`
  conflicts to resolve; describe the new propagation model.
- [ ] Full baseline green; scope guard (no unrelated churn).
