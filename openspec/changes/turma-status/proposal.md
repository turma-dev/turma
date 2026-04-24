## Why

`turma status` currently returns a placeholder string. Operators
running `turma run` regularly need to answer: "what's the state of
feature X right now — what's ready, what's claimed, what's in
`needs_human_review`, which PRs are open, which worktrees are
sitting on disk as failed-triage artifacts?" Today they piece that
together by hand: `bd list --label feature:<name>`, `gh pr list
--head task/<name>/`, `git worktree list`, file listings under
`.worktrees/<name>/`.

A small, read-only, feature-scoped status command closes this gap
with no new architectural risk. It also serves as an
implementation-gate check on the adapter stack's read side before
the next meaningful feature (post-merge advancement) builds
mutation logic on top.

Deliberately v1 scope. No global (cross-feature) status, no merge
or PR annotation writeback, no telemetry / metrics endpoints, no
long-running watch mode. The goal is "one command, one compact
readout, zero mutations."

## What Changes

- New `turma status --feature <name>` subcommand that prints a
  compact, feature-scoped readout of Beads task state + GitHub PR
  state + worktree presence. Single pass over the relevant
  adapter reads; exits 0 on success.
- **No-mutation invariant.** The command never calls `claim_task`,
  `close_task`, `fail_task`, `commit_all`, `push_branch`,
  `open_pr`, `WorktreeManager.setup`, or
  `WorktreeManager.cleanup`. Unit tests assert zero calls to each
  mutating surface on stub adapters.
- `BeadsAdapter` gains `list_feature_tasks_all_statuses(feature)`
  so the status readout can show closed + needs-human-review
  tasks alongside open / in-progress. argv pinned by unit tests
  (`bd list --label feature:<name> --status
  open,in_progress,blocked,deferred,closed --json --limit 0` or
  whatever bd's canonical all-statuses incantation turns out to
  be at Task 1 verification time).
- `PullRequestAdapter` gains
  `list_prs_for_feature(feature, worktree_manager) -> tuple[PrSummary,
  ...]` that batches `gh pr list` against the feature's task
  branches. The adapter already has `find_open_pr_url_for_branch`
  (single-head, single-state); the status readout needs all
  branches and all states (open / merged / closed). argv pinned
  in the implementation task.
- New `src/turma/swarm/status.py` module exposing
  `status_readout(feature, *, services, repo_root) -> str` that
  composes the adapter reads into the rendered text block. Pure
  function — takes `SwarmServices`, returns a string. The CLI
  prints the string; tests can inspect it as data.
- `src/turma/cli.py` `turma status` subcommand replaces the
  placeholder: `--feature <name>` required; loads services via
  `load_swarm_config` + `default_swarm_services` (same wiring as
  `turma run`); prints `status_readout(...)`.
- `README.md` gets a short "Feature Status" subsection under the
  Swarm Execution section covering what the command shows, what
  it doesn't, and a worked-example output.

## Capabilities

### New Capabilities

- `status-readout`: feature-scoped composition of Beads / PR /
  worktree state into an operator-readable text block. Read-only;
  no mutation across any adapter.

### Modified Capabilities

- `beads-adapter` gains an all-statuses feature lister.
- `pull-request-adapter` gains a batched feature PR lister.
- `cli` replaces the `turma status` placeholder with the real
  dispatch.

## Impact

- New files:
  - `src/turma/swarm/status.py` — `status_readout(...)` and the
    rendering helpers.
  - `tests/test_swarm_status.py` — the read-only invariant and
    per-section rendering tests.
- Modified:
  - `src/turma/transcription/beads.py` — add
    `list_feature_tasks_all_statuses`.
  - `src/turma/swarm/pull_request.py` — add
    `list_prs_for_feature`.
  - `src/turma/cli.py` — real `turma status` dispatch.
  - `tests/test_transcription_beads.py` — argv + parsing for the
    new bd lister.
  - `tests/test_swarm_pull_request.py` — argv + parsing for the
    new gh lister.
  - `tests/test_swarm_cli.py` — `turma status --feature` subparser
    and dispatch coverage.
  - `README.md` — Feature Status subsection under Swarm
    Execution.
  - `CHANGELOG.md` — `[Unreleased]` entry.

## Out of Scope

- Cross-feature or global `turma status`. v1 requires `--feature
  <name>`; multi-feature views are a future enhancement.
- Live watch / polling mode (`--watch`). One-shot snapshot only.
- Historical metrics (time-to-complete, retry-rate analytics).
  The status readout is a current-state view, not a telemetry
  surface.
- Post-merge advancement, PR-merged detection, or any write
  action based on observed state. That's the next feature arc
  (post-merge advancement).
- `turma status` that infers or guesses. If adapter calls fail
  with `PlanningError`, surface the error and exit 1; don't
  render a partial readout that silently omits a section.
