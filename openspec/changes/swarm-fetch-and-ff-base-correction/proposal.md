## Why

The 2026-04-26 chained-flow live smoke against
`khanhgithead/turma-run-smoke` failed at iteration 1 with:

```
error: git fetch failed: exit 128
fatal: refusing to fetch into branch 'refs/heads/main' checked
out at '/private/var/folders/.../smoke-stab-XXXXX...'
```

Root cause: the colon-form
`git fetch origin <base_branch>:<base_branch>` that
`swarm-merge-advancement-stabilization` Task 3 shipped cannot
update a currently checked-out branch. Standard `turma run`
usage runs from the repo's active working copy with HEAD on
`<base_branch>` — that's not an edge case, it's the default
state. So the colon-form is broken for the standard usage.

The original design.md rejected the two-call
`fetch + merge --ff-only` form on the grounds that "the merge
step requires HEAD to be on `<base_branch>`." That objection
was a bad optimization target: the standard `turma run` usage
already has HEAD on `<base_branch>`. The colon-form's claimed
"doesn't disturb HEAD" advantage was illusory because git
refuses the colon-form precisely *because* HEAD would be
updated.

Three layers were wrong in lockstep:

1. **Spec choice**: design.md picked the colon-form citing a
   non-existent benefit.
2. **Adapter implementation**: matched the broken spec.
3. **Test coverage**: subprocess-mock tests validated the
   adapter's claimed contract against itself, never exercising
   git's actual checkout-protection behavior.

The live smoke caught the bug on the first invocation —
exactly what the manual-smoke `[ ]` box on
`swarm-merge-advancement-stabilization`'s Task 7 was for.

This is release-blocking for 0.3.0.

## What Changes

- **`GitAdapter.fetch_and_ff_base(repo_root, base_branch)`
  flips to the two-call form**, exactly the alternative the
  prior spec rejected:
  ```
  git -C <repo_root> fetch origin <base_branch>
  git -C <repo_root> merge --ff-only origin/<base_branch>
  ```
  - The fetch updates `refs/remotes/origin/<base_branch>` (a
    remote-tracking ref) — never touches a local branch ref
    so checkout-protection doesn't apply.
  - The merge updates `<base_branch>` (and HEAD) cleanly via
    the same code path normal `git pull` uses.
  - Requires HEAD on `<base_branch>` at call time. That's the
    standard `turma run` precondition; it stops being a
    "downside" once we name it as a precondition.
- **Failure mapping splits along the call boundary**:
  - `git fetch` non-zero exit → network / auth / remote
    error. `PlanningError` preserving stderr with a
    `git fetch failed:` prefix.
  - `git merge --ff-only` non-zero exit, stderr names
    `Not possible to fast-forward` or `non-fast-forward`
    → divergence error. Typed `PlanningError` naming the
    branch and the two `git log <a>..<b>` triage commands.
  - `git merge --ff-only` non-zero exit, other stderr →
    `PlanningError` preserving stderr with a
    `git merge --ff-only failed:` prefix.
- **HEAD-on-`<base_branch>` is a documented precondition,
  not a checked one.** `git merge --ff-only` does not emit
  a clean "HEAD is not on the merge target" stderr signal
  — when the operator is on a feature branch, the merge
  either silently fast-forwards the feature ref (if it's
  an ancestor of origin) or refuses with the same
  divergence signal a real divergent local would produce.
  Detecting this cleanly would require a third
  `git symbolic-ref` call; v1 defers that and documents
  the precondition instead. The smoke runbook's setup
  ensures HEAD is on `<base_branch>` for the supported
  usage. See `design.md` "HEAD-on-<base_branch> is a
  documented precondition, not a checked one" for the
  full rationale.
- **Subprocess-mock tests in `tests/test_swarm_git.py`
  updated** for the two-call argv shape and split error
  mapping. Removed: `test_fetch_and_ff_base_typed_error_on_rejected_substring`
  (the `[rejected]` message was a colon-form artifact and no
  longer applies). Added: a "fetch fails → merge never called"
  ordering test.
- **One real-git integration test** in
  `tests/test_swarm_git_integration.py` (new file): tmpdir
  bare remote + working clone, exercises (a) happy-path
  fast-forward, (b) divergent local rejection, (c) HEAD
  not-on-base detection. This is the gap the live smoke
  caught — subprocess mocks validate our contract, real git
  validates git's contract.
- **`docs/architecture.md` Execution paragraph amended** to
  replace "single-call colon-form" with "two-call
  `fetch + merge --ff-only`" plus a one-sentence note on
  why the colon-form was rejected (git's checkout-protection
  on the destination ref).
- **CHANGELOG `[Unreleased]/Fixed` amended** to reflect the
  corrected Finding 3 implementation. The prior arc's entry
  named the colon-form; this arc names the two-call form
  with the live-smoke discovery as the reason for the
  correction.
- **Live smoke re-run** against
  `khanhgithead/turma-run-smoke` walks Step 3a end-to-end
  one more time. Closes the manual-smoke `[ ]` on the prior
  arc's tasks.md.

## What does NOT change

- **`run_swarm` wiring** — the call site
  (`services.git.fetch_and_ff_base(services.repo_root,
  services.base_branch)`) and its position between preflight
  and reconcile stay as shipped.
- **Print contract** —
  `fetch: skipped (--dry-run)` /
  `fetch: origin/<base> → <base>` lines are unchanged. Same
  semantic, different argv underneath.
- **README + smoke runbook prose** — the user-facing
  description in `README.md`'s "Base-branch sync"
  subsection (HEAD on base, fetch fails loudly on
  divergence, `--dry-run` skips it) is correct as written.
  The smoke runbook's `fetch: origin/<base> → <base>`
  expected log lines reflect the print contract, not the
  underlying argv, so they stay too. `docs/architecture.md`
  IS amended (see "What Changes" above) because that
  document names the specific argv shape.
- **The chained-flow regression test
  (`test_chained_feature_post_merge_advances_dependent`)** —
  remains a stub-level orchestrator-contract regression, not
  a real-git test. The new integration test owns the real-
  git contract; the regression test owns the orchestrator-
  dispatch contract. Different scopes, both kept.
