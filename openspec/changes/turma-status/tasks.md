## Tasks

### 1. Extend BeadsAdapter with all-statuses feature lister

- [ ] **Determine the argv first.** Run `bd list --help` and
      pick the minimal flags that return every feature-tagged
      task regardless of status (`--all`, `--status open,closed`,
      or whatever bd 1.0.2 supports). Document the chosen argv
      in the method docstring with the same argv-pinning test
      pattern used for `list_feature_tasks` /
      `list_ready_tasks`.
- [ ] Add `list_feature_tasks_all_statuses(feature: str) ->
      tuple[BeadsTaskRef, ...]` to `BeadsAdapter` in
      `src/turma/transcription/beads.py`. Factor out the
      `bd list --json` payload-parsing helper if the new method
      would duplicate the logic from `list_feature_tasks` /
      `list_ready_tasks` / `list_in_progress_tasks`; otherwise
      inline it and leave the consolidation for a later sweep.
- [ ] Unit tests in `tests/test_transcription_beads.py`: argv
      pinned; happy path returns mixed-status rows; empty stdout
      → empty tuple; non-JSON / non-array payloads raise
      `PlanningError` with the key-path surfaced; label parsing
      (`feature:<name>`, `turma-type:<t>`,
      `turma-retries:<n>`, `needs_human_review`) unchanged.

### 2. Extend PullRequestAdapter with batched feature PR lister

- [ ] **Determine the argv first.** Try
      `gh pr list --search "head:task/<feature>/" --json
      number,url,state,title,headRefName` in a scratch repo; if
      it returns the right set, use that. If `gh`'s `--search`
      doesn't support the head-prefix filter reliably on
      `gh 2.91.0`, fall back to a per-branch loop that calls
      `gh pr list --head <branch> --state all --json …` for
      each branch from
      `worktree_manager.list_task_branches(feature)`. Document
      the chosen path in the method docstring.
- [ ] Add a frozen `PrSummary` dataclass
      (`number: int`, `url: str`, `state: str`, `title: str`,
      `head_branch: str`) at module scope.
- [ ] Add `list_prs_for_feature(feature: str, worktree_manager:
      WorktreeManager) -> tuple[PrSummary, ...]` to
      `PullRequestAdapter`. Non-zero `gh` exit raises
      `PlanningError` with stderr preserved; non-JSON / non-array
      payloads also raise.
- [ ] Unit tests in `tests/test_swarm_pull_request.py`: argv
      pinned; empty array → empty tuple; single-PR parse;
      multi-PR parse across mixed states (open / closed /
      merged); non-zero exit surfaces stderr; non-JSON /
      non-array rejection.

### 3. Build the status_readout module

- [ ] New module `src/turma/swarm/status.py` exposing
      `status_readout(feature: str, *, services: SwarmServices,
      repo_root: Path) -> str`. Pure function — all state via
      parameters, returns the rendered text block.
- [ ] Compose the adapter reads in the order pinned by
      `design.md`:
      1. All-statuses task list (for counters + orphan filter).
      2. Ready task list.
      3. In-progress task list, with per-task `retries_so_far`
         lookups.
      4. `WorktreeManager.list_task_branches` for the feature.
      5. `PullRequestAdapter.list_prs_for_feature`.
- [ ] Render per the pinned output shape. Each subsection
      emits `(none)` when empty — no conditional skipping, no
      empty gaps. Counters use a stable status-to-bucket
      mapping (ready / in_progress / blocked-or-deferred /
      closed / needs_human_review).
- [ ] Sentinel inspection in the in-progress section reads
      `.task_complete` / `.task_failed` if present. Read is
      lossless — no unlink. A worktree whose `.task_failed` body
      exceeds one line is truncated to the first line in the
      readout; the full file is still on disk for triage.
- [ ] Missing spec dir / `APPROVED` / `TRANSCRIBED.md` render
      **inline** as `no` with a one-line terminal hint. Do not
      raise — the whole point of status is to show state,
      including "no state yet."
- [ ] Adapter / subprocess failures DO raise `PlanningError`
      with the tool's stderr. Partial readouts are forbidden.
- [ ] Expose `status_readout` via `src/turma/swarm/__init__.py`
      re-export so the CLI can `from turma.swarm import
      status_readout`. Drop the placeholder `status_summary`
      from the public re-exports (the CLI's `turma status`
      dispatch is the only caller).

### 4. Tests for status_readout

- [ ] New `tests/test_swarm_status.py`. Pattern:
      `dataclass`-based stubs for every adapter
      (`StubBeadsAdapter`, `StubWorktreeManager`,
      `StubGitAdapter`, `StubPullRequestAdapter`), each tracking
      every method call; fixture helpers build a populated
      `SwarmServices` + realistic feature state.
- [ ] **No-mutation invariant test.** Populate the feature
      fixture with everything (ready + in_progress + closed +
      needs_human_review + open PR + orphan branch) and call
      `status_readout`; assert zero calls to any mutating
      surface on every stub (claim_task / close_task / fail_task
      / setup / cleanup / commit_all / push_branch / open_pr).
      Headline test, matches the reconciliation-module pattern.
- [ ] Task counter correctness across a mixed-status fixture.
- [ ] Per-section rendering tests:
      - Ready section: populated + empty `(none)`.
      - In-progress section: retries label rendered when
        present and absent; worktree present vs absent; each of
        the three sentinel states (complete / failed-with-reason
        / none).
      - Pull requests section: open / closed / merged states all
        rendered; empty case `(none)`.
      - Orphan branches section — strict in_progress-only
        filter matching `reconcile_feature`'s classification
        (the status readout does not redefine the reconciliation
        contract):
        - branch matches an in-progress task → NOT rendered.
        - branch matches a ready task → rendered as orphan (the
          retry case; reconciliation would classify it the same
          way at run-start).
        - branch matches a closed task → rendered as orphan
          (cleanup-residue signal).
        - branch with no corresponding task → rendered as
          orphan.
        - empty case `(none)`.
- [ ] Missing spec dir / APPROVED / TRANSCRIBED.md render as
      `no` with the hint line; command still returns a readout
      (does not raise).
- [ ] Adapter failure (stubbed `PlanningError` on
      `list_feature_tasks_all_statuses` or
      `list_prs_for_feature`) propagates out of
      `status_readout` — no partial output.

### 5. Wire the CLI subcommand

- [ ] Replace the placeholder `turma status` dispatch in
      `src/turma/cli.py` with the real handler: `--feature <name>`
      required; load config via `load_swarm_config`; build
      services via `default_swarm_services`; call
      `status_readout(feature, services=..., repo_root=...)`;
      `print(result)`; return 0. `ConfigError` / `PlanningError`
      map to `error: <msg>` + exit 1 (same channel the other
      commands use).
- [ ] Update the argparse subparser to require `--feature`.
- [ ] `src/turma/swarm/__init__.py` drops `status_summary` from
      the public re-exports (no remaining callers after the CLI
      rewire).

### 6. CLI tests

- [ ] New tests in `tests/test_swarm_cli.py` (or a dedicated
      `tests/test_status_cli.py` if the status tests grow past
      a handful):
      - Subparser registered; `--feature` required; unknown flag
        rejected.
      - Happy path: `main(["status", "--feature", "oauth"])`
        calls through `load_swarm_config`,
        `default_swarm_services`, and `status_readout` with the
        parsed args; prints the rendered block; exits 0.
      - `ConfigError` from the loader exits 1.
      - `PlanningError` from `status_readout` exits 1 with
        `error: <msg>` on stdout.

### 7. Docs + CHANGELOG

- [ ] `README.md`: add a "Feature Status" subsection under
      "Swarm Execution" covering what the readout shows, the
      no-mutation guarantee, and a worked-example block against
      a small transcribed feature.
- [ ] `CHANGELOG.md` `[Unreleased]`: single entry under "Added"
      rolling up the new command + the adapter additions.

### 8. Validation

- [ ] `uv run pytest` — full suite green. Current baseline
      before this change set: 423 tests. Expected additions are
      ~15 across status + adapter + CLI tests.
- [ ] Manual smoke: on the existing `khanhgithead/turma-run-smoke`
      scratch (which already has closed tasks + open PRs from
      prior smoke runs), `uv run turma status --feature smoke-run`
      should render a realistic all-sections output. Document
      any surprises as follow-up tasks; do not silently paper
      over them.
- [ ] No new runtime dependencies in `pyproject.toml`. `bd` and
      `gh` were already prerequisites for `turma run`; `turma
      status` adds no new ones.
