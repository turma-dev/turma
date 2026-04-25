# Turma

Provider-pool-aware multi-agent coding orchestration with spec-driven planning,
Beads task tracking, and resumable swarm execution.

## Status

Early implementation phase. This repo has the Python package layout, OpenSpec
workflow scaffolding, a working `turma init` command, a working `turma plan`
command running a full author/critic loop with an explicit human approval
gate and resume CLI, a working `turma plan-to-beads` command that
transcribes approved plans into a feature-tagged Beads task set, a working
`turma run` single-feature sequential swarm orchestrator (preflight →
reconcile → repair → main loop, one PR per Beads task), baseline CI, and
public architecture documentation.

## What It Is

Turma is designed as a two-phase workflow:

1. Planning: generate and refine OpenSpec artifacts through an author/critic
   loop with explicit human approval.
2. Execution: translate approved tasks into a Beads DAG and route work across
   multiple agent runtimes while tracking task and integration state.

The main design goal is to treat provider rate-limit pools as a routing input
without overstating that pool independence alone solves throughput.

## Repository Layout

```text
.
├── .github/workflows/          # minimal CI
├── .agents/                    # role guidance for author / critic / implementer / reviewer
├── .claude/commands/           # slash commands used in project context
├── openspec/                   # feature specs and changes
├── docs/
│   ├── architecture.md         # public system model
├── src/turma/                  # Python package and CLI
├── tests/                      # automated tests
├── CHANGELOG.md
├── LICENSE
├── README.md
├── turma.example.toml         # committed config template
└── pyproject.toml
```

## CLI Scaffold

Default development workflow:

```bash
uv sync
uv run turma --help
uv run turma init
uv run turma plan --feature oauth-auth
uv run turma run --feature oauth-auth
uv run turma status
```

Current command status:

- `turma init` is implemented
- `turma plan` runs the full author/critic loop with a human approval gate
  and a resume CLI
- `turma plan-to-beads` transcribes an approved plan into a
  feature-tagged Beads task set (requires `bd` and Dolt; see
  Plan-to-Beads below)
- `turma run` drives a single-feature sequential swarm against the
  transcribed Beads DAG (see Swarm Execution below)
- `turma status --feature <name>` prints a read-only readout of a
  feature's current Beads + PR + worktree state (see Feature
  Status below)

`turma init` expects `turma.example.toml` to exist in the target directory. It
creates `turma.toml` from that template and updates `.gitignore` with
Turma-managed entries.

`turma plan --feature <name>` does the following per round:

- reads `planning.author_model` and `planning.critic_model` from `turma.toml`
- requires `.agents/author.md` and `.agents/critic.md`
- on round 1, scaffolds an OpenSpec change with `openspec` and generates
  `proposal.md`, `design.md`, and `tasks.md`
- on round ≥ 2, runs the two-call revision: author first writes
  `response_{N-1}.md` replying to each finding in `critique_{N-1}.md`, then
  regenerates the three artifacts using that response as context
- runs the critic backend to produce a strict `critique_N.md`
- routes on the critic's `## Status:` token: `blocking` → revise,
  `nits_only` / `approved` → await human, malformed → `needs_human_review`
- suspends at `awaiting_human_approval` with the exact resume commands
  printed

Loop budget: `planning.max_rounds` caps the iterations; repeated unresolved
blocking finding IDs across two rounds also route to `needs_human_review`.
Filesystem terminal markers (`APPROVED`, `ABANDONED.md`,
`NEEDS_HUMAN_REVIEW.md`) are authoritative — re-running `turma plan` on an
already-terminal plan is a read-only no-op.

Planning quality depends on the chosen backend/model. Claude-backed planning
is currently the strongest validated path. OpenCode transport is validated,
but provider/model quality varies. Gemini requires the `gemini` CLI
(`npm install -g @google/gemini-cli`).

It does not yet commit changes or orchestrate execution.

## Resume CLI

```bash
uv run turma plan --feature <name> --resume                           # read-only status
uv run turma plan --feature <name> --resume --approve                 # write APPROVED
uv run turma plan --feature <name> --resume --revise "<why>"          # advance into a new round
uv run turma plan --feature <name> --resume --abandon "<why>"         # write ABANDONED.md
uv run turma plan --feature <name> --resume --approve --override "<why>"  # override from needs_human_review
```

`--approve`, `--revise`, and `--abandon` are valid only when the graph is
suspended at `awaiting_human_approval`. `--approve --override` is valid only
when the graph has halted in `needs_human_review`.

## Plan-to-Beads

Once a plan has an `APPROVED` marker, `turma plan-to-beads` translates its
`tasks.md` into a feature-tagged set of Beads tasks with typed entries,
priorities, and dependency edges.

```bash
uv run turma plan-to-beads --feature <name>
uv run turma plan-to-beads --feature <name> --force
```

### Prerequisites

`bd` (Beads) is a Go binary, not a PyPI package. Install it together with
Dolt (Beads' storage backend):

```bash
brew install beads        # pulls Dolt and other required dependencies
```

`turma plan-to-beads` raises a clear error with the `brew install beads`
hint when `bd` is not on PATH. See
https://github.com/steveyegge/beads for non-macOS install paths.

### Behavior

- Gates on the `APPROVED` terminal marker; a plan that is not approved is
  rejected.
- Parses `tasks.md` via the strict parser (see
  `openspec/changes/beads-transcription/design.md` for the grammar).
- Translates parser task types to Beads-native types
  (`impl`/`test` → `task`, `docs` → `chore`, `spec` → `decision`) and
  parser priority to Beads priority (`min(section_number - 1, 4)`; 0 is
  highest).
- Creates each section's Beads task with a `feature:<name>` label for
  downstream filtering, then adds `bd dep add` blocking edges from
  each section to its `blocked-by` predecessors.
- On full success writes `TRANSCRIBED.md` in the change directory
  recording the created Beads task ids in section order.
- Prints a compact summary of the created tasks on stdout.

### `--force` semantics

- With a prior `TRANSCRIBED.md` present: closes the recorded Beads task
  ids in reverse section order, removes the marker, and re-runs the
  pipeline.
- With no marker but feature-tagged Beads orphans present (from a
  prior failed attempt): closes the orphans via
  `bd list --label feature:<name>` and re-runs the pipeline.
- With neither a marker nor orphans: `--force` is a no-op and the
  pipeline runs normally.
- A `TRANSCRIBED.md` that exists but parses to no `- section N: <id>`
  lines is hard-rejected under `--force` to avoid duplicate creation
  against a corrupt marker. Inspect or delete the file manually and
  retry.

### Partial-failure recovery

Turma does not roll back partial Beads state on failure. If an adapter
call fails mid-run, the already-created tasks remain on the Beads side
with their `feature:<name>` label, and no `TRANSCRIBED.md` is written.
A re-run without `--force` detects the feature-tagged orphans during
preflight and surfaces their ids plus two recovery paths:

```bash
# Option A — manual close, then retry from scratch
bd close <id> <id> ...
uv run turma plan-to-beads --feature <name>

# Option B — let --force close the orphans for you
uv run turma plan-to-beads --feature <name> --force
```

Validation commands:

```bash
uv run turma --help
uv run python -m turma --help
uv run pytest
```

## Swarm Execution

Once a feature has been transcribed to Beads, `turma run` drives a
single-feature sequential execution loop that claims ready Beads
tasks, runs a worker agent inside a per-task git worktree, opens one
PR per completed task against the default base branch (`main`), and
stops. Review, merge, and release are human-driven.

```bash
uv run turma run --feature <name>
uv run turma run --feature <name> --max-tasks 1       # smoke one task end-to-end
uv run turma run --feature <name> --backend claude-code
uv run turma run --feature <name> --dry-run           # preflight + reconcile only
```

Config: `turma run` reads the `[swarm]` block from `turma.toml`
for `worker_backend`, `worker_timeout`, `max_retries`,
`worktree_root`, and `base_branch`. CLI flags take precedence —
`--backend` overrides `worker_backend`, and `--max-tasks` is a
per-invocation cap with no config equivalent. Missing or partial
`[swarm]` blocks fall back to the defaults in
`turma.example.toml`.

### Prerequisites

- `bd` (Beads) on PATH (`brew install beads`; see Plan-to-Beads above)
- `git` on PATH
- `gh` (GitHub CLI) on PATH with an authenticated session
  (`gh auth login` once; verified at startup via `gh auth status`)
- `claude` (Claude Code CLI) on PATH for the default
  `claude-code` worker backend. `--dry-run` does not require
  `claude` because the worker is never invoked.
- A transcribed feature: `openspec/changes/<name>/APPROVED` and
  `openspec/changes/<name>/TRANSCRIBED.md` must both exist. Missing
  either halts with a pointer back to `turma plan` or
  `turma plan-to-beads`.

### The one-feature loop

For each ready Beads task, the orchestrator runs:

```
claim → setup_worktree → run_worker → (sentinel) → commit → push → open_pr → mark_pr_open
```

`mark_pr_open` records the PR number on the Beads task via a
`turma-pr:<N>` label and leaves the task in `in_progress` with
its worktree on disk. The matching `close_task` +
`cleanup_worktree` defer to the next `turma run`'s merge
advancement sweep (see below), which fires only after GitHub
reports the PR as merged. This is what keeps dependents from
being claimed against an unmerged base.

Failed steps enter the retry path via `fail_task` on the Beads task.
A worker that claims success but leaves the worktree clean
(`.task_complete` present but `git status --porcelain` empty) is
treated as a failure with a canned reason so a non-editing worker
cannot land an empty commit.

The worker signals completion via filesystem sentinels inside the
worktree:

- `.task_complete` — worker believes the task is done; orchestrator
  commits, pushes, opens a PR, and labels the Beads task with
  `turma-pr:<N>`. Close + cleanup defer to merge advancement.
- `.task_failed` — worker hit an unresolvable blocker; contents are
  the failure reason. Orchestrator calls `fail_task` and leaves the
  worktree on disk for triage.
- No sentinel after worker exit → failure with reason
  `"worker exited without writing a completion marker"`.

### Retry budget and halt conditions

Retry state lives on the Beads task:

- `turma-retries:<n>` label — attempt counter, absent means zero.
- `needs_human_review` label — added on budget exhaustion so
  `list_ready_tasks` filters the task out of future listings.

On failure, the orchestrator reads `retries_so_far` and calls
`fail_task(reason, retries_so_far, max_retries)`. Budget remaining
→ the task returns to `open` for a future re-attempt. Budget
exhausted → the orchestrator halts the whole run so the operator
can triage via `bd list --label needs_human_review`.

`max_tasks` caps the outer loop at N successfully-claimed tasks
(claim races do not consume budget). Default is unbounded.

### Reconciliation on resume

Reconciliation always runs at startup — including `--dry-run` —
before the main loop. It walks the Beads `in_progress` set and
classifies each task into one of six finding types based on the
worktree filesystem and GitHub PR state:

| Finding | Cause | Repair |
| --- | --- | --- |
| `missing-worktree` | Beads says in_progress, worktree absent | release the claim (counts against the retry budget) |
| `completion-pending` | `.task_complete` present, no open PR | commit + push + open_pr + mark_pr_open |
| `completion-pending-with-pr` | `.task_complete` present, PR already open | mark_pr_open (no new PR; close + cleanup defer to merge advancement) |
| `failure-pending` | `.task_failed` present | fail_task with the worker's reason (worktree left for triage) |
| `stale-no-sentinels` | worktree + branch exist, no sentinel | halt before the main loop; operator decides |
| `orphan-branch` | `task/<feature>/*` branch with no in_progress task | log only; operator triage |

Reconciliation itself is read-only: every mutation (`fail_task`,
`close_task`, `commit`, `push`, `gh pr create`) is performed by the
repair phase in the main loop, and `--dry-run` skips the repair
phase entirely.

### Merge advancement

Between the repair phase and the main loop, `turma run` sweeps
every `in_progress` task that carries a `turma-pr:<N>` label and
queries the PR's GitHub state via `gh pr view <N> --json state`.
The dispatch is read-only-then-mutate — one `gh` read per
labelled task, then exactly one of:

| `gh` returns | Action |
| --- | --- |
| `MERGED` | `unmark_pr_open` + `close_task` + `cleanup_worktree` — dependents become claimable on the same `turma run` |
| `OPEN` | leave alone (drafts return `OPEN` from `--json state` and fall through this branch unchanged) |
| `CLOSED` without merge | `fail_task` with reason `"PR #<N> closed without merge"` so the retry budget applies |
| PR not found / 404 | halt with `PlanningError`; the label is stale and the operator triages |

Tasks without a `turma-pr:<N>` label are skipped (no `gh` call),
matching the label-gated dispatch. `--dry-run` performs the
PR-state reads but no mutations.

The sweep prints one line per processed task, prefixed with
`merge-advancement:` so the source is unambiguous in the run
log.

### Failure modes (CLI)

| `error: <msg>` starts with | Cause |
| --- | --- |
| `feature 'X' is not APPROVED` | no `APPROVED` marker; run `turma plan` |
| `feature 'X' has not been transcribed` | no `TRANSCRIBED.md`; run `turma plan-to-beads` |
| `bd CLI not found` | `bd` missing from PATH |
| `gh CLI not found` | `gh` missing from PATH |
| `gh session not authenticated` | run `gh auth login` |
| `stale worktree for <id> has no sentinels` | reconcile caught ambiguous state; operator decides |
| `retry budget exhausted on <id>` | task hit `max_retries`; triage with `bd list --label needs_human_review` |

### Worked example

Against a feature already transcribed to Beads (see Plan-to-Beads
above):

```bash
# First run — opens a PR for task 1. The Beads task stays
# in_progress with a `turma-pr:<N>` label; the worktree at
# .worktrees/oauth-auth/<bd-id>/ stays on disk awaiting merge.
# A dependent task is NOT yet claimable.
uv run turma run --feature oauth-auth --max-tasks 1

# Reviewer merges the PR on GitHub (or via `gh pr merge <N>`).
# Nothing local needs to change — the merge is the only signal
# the orchestrator depends on.

# Second run — the merge advancement sweep observes MERGED,
# unmarks the label, closes the task, and removes the
# worktree. The dependent task is now ready and gets claimed
# in the same invocation.
uv run turma run --feature oauth-auth

# Operator triage after budget exhaustion.
bd list --label needs_human_review
bd show <id>
```

A detailed end-to-end smoke procedure against real `bd` + `gh` +
`claude` lives in [`docs/smoke-turma-run.md`](docs/smoke-turma-run.md).

### Feature status

`turma status --feature <name>` prints a read-only readout of the
feature's current Beads + GitHub PR + worktree state. The
command never mutates anything — no `claim_task`, no
`close_task`, no `fail_task`, no commits, no pushes, no
`gh pr create`. Useful between `turma run` invocations to
answer "where is this feature right now?"

```bash
uv run turma status --feature oauth-auth
```

The readout has six sections, in fixed order, each with a
`(none)` placeholder when empty:

- **feature header** — spec dir presence, `APPROVED` /
  `TRANSCRIBED.md` flags, with next-step hints (e.g.
  "run `turma plan --feature ...` first") inline when missing.
  A missing spec dir does not raise; the readout still prints.
- **task counters** — `ready` / `in_progress` /
  `blocked / deferred` / `closed` / `needs_human_review`.
  Buckets are mutually exclusive: a task with the
  `needs_human_review` label is counted there regardless of
  its bd status; `open` tasks not in `bd ready`'s view are
  counted as dependency-blocked.
- **ready tasks** — claimable right now.
- **in-progress tasks** — per-task: `retries: <n> / <max>`,
  worktree presence, sentinel state
  (`complete | failed: "<reason>" | none`). The
  `.task_failed` body is truncated to the first line in the
  readout; the full file stays on disk for triage. When the
  task carries a `turma-pr:<N>` label (recorded by the
  success path / repair phase), an extra
  `pr: #<N> (<state>) <url>` line is added below the
  sentinel — state and URL come from a live `gh pr view`,
  not the cached label, so MERGED PRs awaiting the next
  `turma run` sweep are visible here.
- **pull requests** — every PR for `task/<feature>/*` head
  branches across all states (`OPEN` / `MERGED` / `CLOSED` /
  `DRAFT`).
- **orphan branches** — local task branches whose Beads task
  isn't `in_progress`. Matches `reconcile_feature`'s
  classification exactly; ready-task retry branches will
  appear here until the next `turma run` re-claims them.

Worked example against a feature mid-flight (synthetic; absolute
paths and PR URLs depend on your repo):

```text
$ uv run turma status --feature oauth-auth
feature: oauth-auth
  spec: openspec/changes/oauth-auth/
  approved: yes
  transcribed: yes

tasks:
  ready:              2
  in_progress:        1
  blocked / deferred: 1
  closed:             3
  needs_human_review: 0

ready tasks:
  bd-oauth-4 — Wire token refresh
  bd-oauth-5 — Add session expiry tests

in-progress tasks:
  bd-oauth-3 — Persist sessions in Redis
    retries: 0 / 1
    worktree: <repo>/.worktrees/oauth-auth/bd-oauth-3/ (present)
    sentinel: failed: "redis client connection refused"

pull requests:
  #14 OPEN — [impl] Token issuance endpoint
    head: task/oauth-auth/bd-oauth-1
    url:  https://github.com/your-org/your-repo/pull/14
  #13 MERGED — [impl] Add OAuth provider config
    head: task/oauth-auth/bd-oauth-2
    url:  https://github.com/your-org/your-repo/pull/13

orphan branches:
  (none)
```

Reading it: 3 closed (PR #13 already merged) + 1 in-progress
(`bd-oauth-3`'s worker wrote `.task_failed` with the first-line
reason rendered inline; the full body is still on disk under the
worktree for triage) + 2 ready + 1 dependency-blocked (counted in
`blocked / deferred`). One PR is still open against the
in-flight task; no branches without a corresponding active task,
so orphan branches is `(none)`.

Adapter failures (`bd list` non-zero exit, `gh pr list`
non-zero exit, etc.) raise `PlanningError` and exit 1 with
`error: <msg>` on stdout — no partial readout printed.

## Core Docs

- [Architecture](docs/architecture.md)
- [Changelog](CHANGELOG.md)

## Next Implementation Steps

- post-merge advancement: detect when a `turma run`-opened PR has
  been merged and unblock dependent Beads tasks automatically
- parallel task execution + per-task backend routing (`worker-backend:<id>` labels)
- Codex / OpenCode / Gemini worker implementations
- a `turma run --clean <feature>` flag to bulk-remove failed worktrees
  and branches

## License

MIT
