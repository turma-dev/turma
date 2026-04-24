## Scope

Single-feature, read-only status readout. Takes a `--feature
<name>` just like `turma run`, produces one compact text block on
stdout, exits 0 on success / 1 on preflight or adapter failure.
Mirrors `turma run`'s preflight + config-loading discipline so an
operator who can run one can run the other.

No global status, no watch loop, no historical metrics, no
per-task drilldown flags in v1 — the whole command is a flat
readout. If the readout needs to be filtered or shaped, the
operator pipes the output.

## No-mutation invariant

The command must never:

- call `BeadsAdapter.claim_task`, `close_task`, `fail_task`
- call `GitAdapter.commit_all`, `push_branch`
- call `PullRequestAdapter.open_pr`
- call `WorktreeManager.setup`, `WorktreeManager.cleanup`

Tests pin this explicitly on stub adapters that track every
method call. A future reader adding behavior to the status module
who accidentally wires a mutating adapter path will trip a test
failure.

Reads are allowed and expected:

- `BeadsAdapter.list_feature_tasks_all_statuses`, `list_ready_tasks`,
  `list_in_progress_tasks`, `retries_so_far` (per-task, only for
  the in-progress set).
- `PullRequestAdapter.list_prs_for_feature` — batched `gh pr list`.
  Uses no mutation; the existing `find_open_pr_url_for_branch` is
  read-only too but scoped to a single branch+state, insufficient
  for the dashboard.
- `WorktreeManager.worktree_path_for`, `branch_name_for`,
  `list_task_branches` — all already present as pure/read-only
  helpers added in the swarm-orchestration arc.
- Filesystem: `Path.exists()` on worktree directories and the
  spec's `APPROVED` / `TRANSCRIBED.md` markers.

## Command surface

```
turma status --feature <name>
```

`--feature <name>` is required. No other flags in v1 — matches
the pattern in `turma plan-to-beads` where the feature is the
entire scope. Exit codes: 0 on a rendered readout, 1 on
`PlanningError` / `ConfigError` via the same `error: <msg>`
channel the other commands use.

## Output sections

Pinned shape — the status readout's exact sections and ordering.
Tests assert each section is present and correctly populated.

```
feature: <name>
  spec: openspec/changes/<name>/
  approved: yes | no
  transcribed: yes | no

tasks:
  ready:              <N>
  in_progress:        <N>
  blocked / deferred: <N>
  closed:             <N>
  needs_human_review: <N>

ready tasks:
  <id> — <title>
  <id> — <title>
  (none)

in-progress tasks:
  <id> — <title>
    retries: <n> / <max>
    worktree: .worktrees/<name>/<id>/ (present | absent)
    sentinel: complete | failed: "<reason>" | none
  (none)

pull requests:
  #<N> <STATE> — <title>
    head: task/<name>/<id>
    url:  <url>
  (none)

orphan branches:
  task/<name>/<id>  (no active task)
  (none)
```

Each subsection's "(none)" placeholder keeps the output regular
— no conditional skipping, no empty gaps. The "tasks" counter
block is the compact at-a-glance summary; the lists below it
give the operator the actionable detail.

### Section specifics

- **spec**: resolves `repo_root / "openspec" / "changes" /
  <feature>`. If absent, the status readout still renders (it's a
  read-only view of current state) but flags approved=no and
  transcribed=no with a terminal hint: "feature has no spec
  directory — run `turma plan --feature <name>`".
- **approved / transcribed**: exists-checks on the `APPROVED` and
  `TRANSCRIBED.md` files in the change directory. Matches the
  preflight in `turma run` but does not halt on absence.
- **task counters**: computed from
  `list_feature_tasks_all_statuses(feature)` with a stable
  status-to-bucket mapping. `needs_human_review` is derived from
  the label, not a distinct bd status, so the counter iterates
  labels too.
- **ready tasks**: from `list_ready_tasks(feature)`. Empty →
  "(none)".
- **in-progress tasks**: from `list_in_progress_tasks(feature)`.
  Per-task retries come from `retries_so_far(task_id)`;
  `max_retries` from the loaded `SwarmConfig`. Worktree presence
  checks `worktree_manager.worktree_path_for(...).is_dir()`;
  sentinel inspection reads `.task_complete` / `.task_failed`
  contents without mutating them.
- **pull requests**: from
  `PullRequestAdapter.list_prs_for_feature(feature,
  worktree_manager)`. Covers any state (open / closed / merged).
  If a PR is in a non-standard state, render `STATE` as bd
  returns it so operator triage is concrete.
- **orphan branches**: from
  `worktree_manager.list_task_branches(feature)` minus the branch
  names corresponding to any task returned by the all-statuses
  bd list. "No active task" here is the broadest definition —
  including closed tasks — so a genuinely abandoned branch
  surfaces even if the task eventually closed out elsewhere.

## Adapter surface additions

Two small additions, both read-only, both argv-pinned by tests.

### `BeadsAdapter.list_feature_tasks_all_statuses`

```python
def list_feature_tasks_all_statuses(
    self, feature: str
) -> tuple[BeadsTaskRef, ...]:
    """List every feature-tagged task regardless of status.

    Unlike `list_feature_tasks` (which defaults to `open` per
    bd's `list` semantics), this method returns closed and any
    other-status rows too so the status readout can show a
    complete counter block and surface recently-closed tasks
    the operator just merged a PR for."""
```

argv TBD at the implementation task, matching bd 1.0.2's
actual multi-status flag shape (`--status open,closed` or
`--all` — verified at Task 1 time the same way Task 2 of
`swarm-orchestration` verified the ready/claim argv).

### `PullRequestAdapter.list_prs_for_feature`

```python
@dataclass(frozen=True)
class PrSummary:
    number: int
    url: str
    state: str
    title: str
    head_branch: str

def list_prs_for_feature(
    self, feature: str, worktree_manager: WorktreeManager
) -> tuple[PrSummary, ...]:
    """Return one `PrSummary` per PR whose head branch matches
    `task/<feature>/*`, across all states.

    Uses the batched `gh pr list` with a head-prefix filter.
    Non-zero exit raises `PlanningError` with `gh` stderr.
    """
```

`worktree_manager` is passed so the method can derive the
expected branch-name prefix from `worktree_manager.branch_name_for`
rather than re-hardcoding the `task/<feature>/<id>` convention.

Exact argv verified at implementation time:
- Candidate 1: `gh pr list --search "head:task/<feature>/"
  --json number,url,state,title,headRefName`
- Candidate 2: iterate known branches and call
  `gh pr list --head <branch> --state all --json …` per
  branch.

Candidate 1 is one subprocess call; 2 is one-per-branch. Task 2
picks whichever works reliably with `gh` 2.91.0+.

## Error surface

All failures use `PlanningError` consistent with the rest of the
CLI:

- Missing `turma.toml` → `ConfigError` from
  `load_swarm_config` → mapped to exit 1 with the same
  `error: turma.toml not found. Run \`turma init\` first.`
  wording as `turma run`.
- Missing external CLI (`bd` or `gh`) → `PlanningError` from
  `default_swarm_services` construction → exit 1.
- `bd list` / `gh pr list` non-zero exit during the readout →
  `PlanningError` with the tool's stderr. The readout does NOT
  render a partial view; either the whole readout or a specific
  error.
- Missing feature directory / markers are rendered **inside** the
  readout (e.g. `approved: no`), not raised — the status
  command's job is to show state, including "no state yet."

## Tests

All adapter interactions go through stubs; no live subprocess in
unit tests. Per-section tests assert:

1. **No-mutation invariant** — the full readout against a populated
   feature state calls zero mutating methods on every stub
   adapter. This is the headline test; it's the same shape as
   `test_reconciliation_never_calls_any_mutation_surface` from
   the reconciliation change set.
2. **Task counters correct** — counts from a fixture that mixes
   ready / in_progress / closed / needs_human_review match the
   readout.
3. **Ready section** — populated / empty cases.
4. **In-progress section** — retries label rendered correctly;
   worktree present / absent differentiated; sentinel rendered
   for each of the three cases (complete / failed-with-reason /
   none).
5. **Pull requests section** — open / closed / merged states all
   rendered; empty case shows `(none)`.
6. **Orphan branches** — present / absent cases; branch name
   matches a closed task in the all-statuses list → rendered as
   orphan (matches the broad definition above); branch matches
   an in-progress task → NOT rendered (operator already has the
   in-progress section for that).
7. **Missing spec dir** — approved/transcribed render as `no`
   with the hint line.
8. **CLI** — `turma status` subparser registered, `--feature`
   required, `PlanningError` → exit 1, happy path calls through
   `status_readout` with the parsed args.

## Open items deferred past v1

- Machine-readable output (`--json`). v1 is text only; JSON is
  natural for a follow-up once the text fields have soaked.
- Filtering flags (`--state ready`, `--only-in-progress`). v1 is
  a single-shape readout.
- Cross-feature summary (`turma status` with no flag). The
  feature-scoped discipline comes from `turma run`'s shape and
  is worth preserving here. A global view is a separate
  command.
- Watch mode / polling. Pipe to `watch -n 10 turma status
  --feature <name>` if needed.
