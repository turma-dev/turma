## Scope

Narrow correction to `swarm-merge-advancement-stabilization`'s
Finding 3 implementation. Same goal (refresh local
`<base_branch>` from origin before reconcile), corrected
implementation. Triggered by the 2026-04-26 live smoke
failure.

Out of scope:

- The orchestrator's call-site wiring, `--dry-run` skip
  semantics, and print lines all stay as the prior arc
  shipped them.
- The deferred orphan-branch decision is still parked.
- A HEAD-independent implementation (lower-level ref-update
  flow, etc.) is a future concern. v1 takes the standard
  `git fetch + merge --ff-only` path because operators
  always run from the active working copy.

## Why the colon-form failed live, despite passing every test

`git fetch origin <ref>:<ref>` with `<ref>` = currently
checked-out branch:

```
fatal: refusing to fetch into branch 'refs/heads/<ref>' checked
out at '<workdir>'
```

Git's checkout-protection: a fetch into a local branch ref
would update HEAD without going through checkout, which git
refuses unconditionally. The colon-form's claimed advantage
("doesn't disturb HEAD") was the inverse of the actual
behavior: the form fails *because* the destination is HEAD.

The subprocess-mock tests didn't catch this because the
`mock_run.return_value` always returned a 0-exit
`CompletedProcess` for the colon-form argv; no test exercised
the actual git binary against a real repo.

## Why fetch + merge --ff-only beats the colon-form

```
git -C <repo_root> fetch origin <base_branch>
git -C <repo_root> merge --ff-only origin/<base_branch>
```

- **fetch** updates `refs/remotes/origin/<base_branch>` (a
  remote-tracking ref). Local branch refs and HEAD are
  untouched. Works regardless of which branch is checked out.
- **merge --ff-only** updates the currently checked-out
  branch ref + HEAD when origin's tip is a fast-forward.
  Same code path `git pull` uses internally. Refuses to
  rewrite history when local has diverged (operator triages).

The "downside" the prior spec named — "merge requires HEAD on
`<base_branch>`" — is the actual standard `turma run`
precondition. The smoke runbook's setup section already does
`cd "$WORKDIR"` (the active working copy) before every
`turma run`. v1 codifies HEAD-on-base as a precondition,
guarded by a `git symbolic-ref` precheck (see "Why the
HEAD precheck is in scope" below) rather than left
unchecked.

## Adapter contract

```python
def fetch_and_ff_base(
    self, repo_root: Path, base_branch: str
) -> None:
    """Fast-forward local <base_branch> from origin.

    Three argv calls in order:
      git -C <repo_root> symbolic-ref --short HEAD
      git -C <repo_root> fetch origin <base_branch>
      git -C <repo_root> merge --ff-only origin/<base_branch>

    The first call reads the current branch name. If it's
    not <base_branch>, the method raises a typed error
    BEFORE any remote I/O. This guards the "operator on a
    feature branch" footgun (silent feature-branch FF) —
    see the precheck-rationale subsection below.
    """
```

### Failure mapping

| Step | Exit signal | Adapter response |
| --- | --- | --- |
| symbolic-ref returns name != `<base_branch>` | HEAD on feature / wrong branch | typed `PlanningError("HEAD is on <current>; turma run must run from a working copy with <base_branch> checked out. cd into the repo's <base_branch> checkout and re-run.")`. Fetch + merge NOT invoked. |
| symbolic-ref non-zero (detached HEAD, missing repo, etc.) | detached HEAD or other | typed `PlanningError("HEAD is detached or unreadable: <stderr>; turma run requires <base_branch> checked out.")`. Fetch + merge NOT invoked. |
| fetch non-zero | network / auth / remote | `PlanningError("git fetch failed: ...", stderr_preserved)`. Merge NOT invoked. |
| merge non-zero, stderr names `Not possible to fast-forward` or `non-fast-forward` | divergent local | typed `PlanningError("local <base_branch> has diverged from origin/<base_branch>; refusing to fast-forward. Triage with git log <a>..<b> ...")` |
| merge non-zero, other | unknown | `PlanningError("git merge --ff-only failed: ...", stderr_preserved)` |

The split lets operators read the surface error and know
which step failed without inspecting subprocess output. The
precheck-step rows mean a misconfigured HEAD is named
explicitly with a `cd` instruction — operators don't have
to interpret a divergence error to figure out the actual
cause.

### Why the HEAD precheck is in scope

Without the precheck, `fetch_and_ff_base` has known silent-
corruption behavior when the operator's HEAD is on a feature
branch:

- If the feature branch is an ancestor of
  `origin/<base_branch>`, `git merge --ff-only origin/<base_branch>`
  silently fast-forwards the **feature branch** to
  origin's tip. The operator's local work isn't lost in
  the data-loss sense (commits stay reachable), but the
  named feature ref is silently moved off them.
- If the feature branch has commits that diverge from
  `origin/<base_branch>`, the merge refuses with the same
  `Not possible to fast-forward` signal divergence
  produces — operators see a "diverged" error that is
  technically correct but doesn't name the underlying
  cause.

`git merge --ff-only` doesn't emit a clean "HEAD is not on
the merge target" stderr signal we can match on after the
fact. The prior draft of this design imagined matching on
`merge: <base> - not something we can merge`, but that
phrase actually fires when the target ref doesn't exist
(e.g. origin has no `<base_branch>`), not when HEAD is on
a different branch.

The fix the prior draft deferred — `git symbolic-ref
--short HEAD` before the fetch+merge — is cheap (no remote
I/O), deterministic, and returns a parseable branch name on
success or non-zero with stderr `fatal: ref HEAD is not a
symbolic ref` on detached HEAD. Comparing the returned name
to `<base_branch>` literal-string-wise is unambiguous.

Pulling the precheck into v1 scope:

- **Cost**: one extra subprocess.run call per `turma run`
  invocation (sub-millisecond). Two new mock tests, one
  new integration test.
- **Benefit**: the silent feature-branch FF documented
  above becomes impossible. Operators who run from the
  wrong working copy get an explicit, actionable error
  instead of either silent corruption or a divergence
  message that mis-names the cause.
- **Why the prior draft deferred it**: I underweighted
  the silent-corruption blast radius. The reviewer
  flagged the gap during the second-round spec review;
  this third revision pulls it in.

### Implementation notes

- Both calls use `subprocess.run(... capture_output=True,
  text=True)` directly (not via `_run`) so the failure-
  mapping inspects exit code + stderr cleanly. Same pattern
  as the colon-form implementation; only the argv shapes and
  branching change.
- The fetch step succeeding doesn't print anything — the
  print line `fetch: origin/<base> → <base>` fires from the
  orchestrator after both subprocesses return. Adapter
  signals success by returning None; failure by raising.

## Tests

Two layers, both required:

### Subprocess-mock layer (existing, updated)

`tests/test_swarm_git.py` already has six
`fetch_and_ff_base` tests. Updates:

- `test_fetch_and_ff_base_pins_argv_shape`: assert TWO
  `subprocess.run` calls in order (fetch then merge), with
  the new argvs.
- `test_fetch_and_ff_base_happy_path_returns_none`: same,
  both calls return zero exit.
- `test_fetch_and_ff_base_typed_error_on_non_fast_forward`:
  fetch returns 0; merge returns non-zero with
  `Not possible to fast-forward` in stderr. Assert typed
  divergence error.
- `test_fetch_and_ff_base_typed_error_on_rejected_substring`:
  **deleted** — the `[rejected]` phrasing was a colon-form
  artifact.
- `test_fetch_and_ff_base_generic_error_preserves_stderr`:
  split into two cases — fetch network failure (merge never
  runs) and merge generic failure.
- `test_fetch_and_ff_base_branch_name_interpolated_into_typed_error`:
  retained, against the new merge stderr.
- New: `test_fetch_and_ff_base_skips_merge_when_fetch_fails`
  — fetch non-zero → merge subprocess not invoked. Pin the
  ordering.
- New: `test_fetch_and_ff_base_typed_error_when_head_not_on_base`
  — merge stderr mentions HEAD/branch mismatch → typed
  PlanningError pointing at `cd`.

### Real-git integration layer (new file)

`tests/test_swarm_git_integration.py` shells out to the
actual `git` binary against a tmpdir. Skip if `git` not on
PATH (CI shouldn't hit this; documented for completeness).

Three tests:

- **Happy path**: tmpdir bare remote, clone, commit on
  remote (via a second working clone), `fetch_and_ff_base`
  fast-forwards local main. Assert local HEAD matches the
  new origin/main tip. **This is the case the colon-form
  failed.** It exercises real git with HEAD on the
  destination ref — exactly the scenario subprocess mocks
  couldn't reach.
- **Divergent local**: bare remote at commit X. Working
  clone diverges (commit Y on local main, never pushed).
  `fetch_and_ff_base` raises typed PlanningError naming the
  branch.
- **HEAD on feature branch**: working clone, check out a
  new branch off main (no commits required — even an
  ancestor case must refuse). `fetch_and_ff_base("main")`
  raises typed `PlanningError` naming the current branch
  and pointing the operator at `cd`. Critically: assert
  the feature branch ref is unchanged after the failed
  call. This is the silent-corruption case the precheck
  is in scope to prevent — without the precheck the
  feature ref would silently FF to origin's tip; with the
  precheck the ref is untouched.

The integration test's value is exactly what the smoke
caught: it exercises git's actual checkout-protection,
symbolic-ref, ref-update, and merge behavior. Mocks
couldn't.

## Error surface (recap)

All failures continue to raise `PlanningError`:

- `HEAD is on <current>; turma run must run from a working
  copy with <branch> checked out. cd into the repo's
  <branch> checkout and re-run.` — symbolic-ref returned a
  branch other than `<base_branch>`.
- `HEAD is detached or unreadable: <stderr>; turma run
  requires <branch> checked out.` — symbolic-ref non-zero
  exit.
- `git fetch failed: <stderr>` — network/auth/remote.
- `local <branch> has diverged from origin/<branch>; ...` —
  fast-forward-impossible because local has commits
  origin doesn't.
- `git merge --ff-only failed: <stderr>` — anything else
  the merge step rejects.

The first two surface the HEAD-precheck failures; the rest
surface fetch and merge step failures. The precheck means
operators on a misconfigured working copy get an explicit
error before any remote I/O.

## Migration notes

- **No code-side migration**. The adapter signature stays
  `fetch_and_ff_base(repo_root, base_branch)`. The
  orchestrator call site is unchanged.
- **Test migration** is internal to the adapter test file:
  one test deleted, several adjusted, several added (HEAD
  precheck cases plus the existing fetch/merge cases).
  Plus the new integration test file with three cases.
- **Doc migration**: a paragraph in `docs/architecture.md`
  Execution section currently says "single-call colon-form";
  amend to "three-call symbolic-ref + fetch +
  merge --ff-only" with a one-line mention of why the
  colon-form was rejected (git's checkout-protection on
  the destination ref) and why the precheck is in scope
  (preventing silent feature-branch FF when operators
  violate the working-copy precondition). The README
  "Base-branch sync" subsection's user-facing description
  (HEAD must be on the base branch, fetch fails loudly on
  divergence, --dry-run skips it) is still correct; no
  changes needed there. The CHANGELOG `[Unreleased]/Fixed`
  entry from the prior arc gets one sentence amended to
  name the three-call form and the precheck.

## Open items deferred

- **HEAD-independent implementation**. A future arc could
  use lower-level ref-update plumbing
  (`git update-ref refs/heads/<base> origin/<base>` with a
  detached-HEAD dance) to make the adapter work regardless
  of HEAD. v1 explicitly does NOT do this — operators always
  run from the active working copy, so the simpler
  precheck-guarded shape wins.
- **Auto-rebase on divergence**. v1 still refuses to rewrite
  history. A `--rebase` flag is a separate workflow decision.
- **Detecting "operator is in a sub-worktree of the repo,
  not the main checkout"**. Out of scope; if it bites
  someone, file a follow-up.
