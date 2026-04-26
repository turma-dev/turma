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

## Why the two-call form is the right v1 path

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
`turma run`. v1 codifies HEAD-on-base as a documented
precondition rather than working around it.

## Adapter contract

```python
def fetch_and_ff_base(
    self, repo_root: Path, base_branch: str
) -> None:
    """Fast-forward local <base_branch> from origin.

    Two argv calls in order:
      git -C <repo_root> fetch origin <base_branch>
      git -C <repo_root> merge --ff-only origin/<base_branch>

    Precondition: HEAD must be on <base_branch>. The
    standard `turma run` invocation runs from the repo's
    active working copy with main checked out. If HEAD is
    on a feature branch, the merge step refuses with a
    typed error pointing the operator at `cd`.
    """
```

### Failure mapping

| Step | Exit signal | Adapter response |
| --- | --- | --- |
| fetch non-zero | network / auth / remote | `PlanningError("git fetch failed: ...", stderr_preserved)`. Merge step is not run. |
| merge non-zero, stderr names `Not possible to fast-forward` or `non-fast-forward` | divergent local | typed `PlanningError("local <base_branch> has diverged from origin/<base_branch>; refusing to fast-forward. Triage with git log <a>..<b> ...")` |
| merge non-zero, stderr names `merge: <base_branch> - not something we can merge` or HEAD-name mismatch | HEAD not on `<base_branch>` | typed `PlanningError("HEAD is not on <base_branch>; cannot fast-forward. cd into the repo's active working copy and re-run.")` |
| merge non-zero, other | unknown | `PlanningError("git merge --ff-only failed: ...", stderr_preserved)` |

The split lets operators read the surface error and know
which mechanism failed without inspecting which subprocess
ran.

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
  new origin/main tip.
- **Divergent local**: bare remote at commit X. Working
  clone diverges (commit Y on local main, never pushed).
  `fetch_and_ff_base` raises typed PlanningError naming the
  branch.
- **HEAD on feature branch**: working clone, checkout a
  feature branch, `fetch_and_ff_base("main")` raises typed
  "HEAD not on main" PlanningError.

The integration test's value is exactly what the smoke
caught: it exercises git's actual checkout-protection,
ref-update, and merge behavior. Mocks couldn't.

## Error surface (recap)

All failures continue to raise `PlanningError`:

- `git fetch failed: <stderr>` — network/auth/remote.
- `local <branch> has diverged from origin/<branch>; ...` —
  fast-forward-impossible due to local commits ahead of
  origin in a non-FF way.
- `HEAD is not on <branch>; cannot fast-forward. cd into the
  repo's active working copy and re-run.` — operator
  invoked from a feature-branch working copy.
- `git merge --ff-only failed: <stderr>` — anything else
  the merge step rejects.

No new error categories beyond what the prior spec promised;
this rearranges which subprocess call surfaces which class.

## Migration notes

- **No code-side migration**. The adapter signature stays
  `fetch_and_ff_base(repo_root, base_branch)`. The
  orchestrator call site is unchanged.
- **Test migration** is internal to the adapter test file:
  one test deleted, several adjusted, two added. Plus the
  new integration test file.
- **Doc migration**: a paragraph in `docs/architecture.md`
  Execution section currently says "single-call colon-form";
  amend to "two-call fetch + merge --ff-only" with a one-
  line mention of why (`git fetch <ref>:<ref>` refuses on
  checked-out HEAD). The README "Base-branch sync"
  subsection's user-facing description (HEAD must be on the
  base branch, fetch fails loudly on divergence, --dry-run
  skips it) is correct as written; no changes needed there.
  The CHANGELOG `[Unreleased]/Fixed` entry from the prior
  arc gets one sentence amended to name the two-call form.

## Open items deferred

- **HEAD-independent implementation**. A future arc could
  use lower-level ref-update plumbing
  (`git update-ref refs/heads/<base> origin/<base>` with a
  detached-HEAD dance) to make the adapter work regardless
  of HEAD. v1 explicitly does NOT do this — operators always
  run from the active working copy, so the simpler shape
  wins.
- **Auto-rebase on divergence**. v1 still refuses to rewrite
  history. A `--rebase` flag is a separate workflow decision.
- **Detecting "operator is in a sub-worktree of the repo,
  not the main checkout"**. Out of scope; if it bites
  someone, file a follow-up.
