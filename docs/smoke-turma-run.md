# `turma run` Smoke Procedure

Task 9 of `openspec/changes/swarm-orchestration/` closes out the
change set with an end-to-end validation against real `bd` + `gh` +
`claude` installs. Unit and integration suites under
`tests/test_swarm_*.py` (100+ tests) cover the adapter argv shape,
reconciliation classification, repair-phase dispatch, main-loop
state transitions, budget enforcement, and CLI wiring — all with
subprocess stubs. This document is the complementary manual smoke
that exercises the real toolchain.

## Prerequisites

- `bd` 1.0.2+ on PATH (`brew install beads` pulls Dolt as a
  dependency).
- **macOS only: GNU `timeout` on PATH.** `bd 1.0.2`'s git
  pre-commit hook calls `timeout 300 bd hooks run pre-commit`.
  macOS's BSD userland ships no `timeout` binary, so the hook
  blocks forever against bd init's own lock and `bd init` hangs
  indefinitely. Install coreutils and expose `gtimeout` as
  `timeout`:
  ```bash
  brew install coreutils
  ln -sf "$(which gtimeout)" /opt/homebrew/bin/timeout
  ```
  Skip this step on Linux — `coreutils`'s `timeout` is already the
  system binary.
- `git` on PATH.
- `gh` on PATH with an authenticated session
  (`gh auth status` returns 0). The GitHub CLI credential helper
  handles HTTPS pushes; ssh-agent is the other supported path.
- `claude` (Claude Code CLI) on PATH for the default worker
  backend. `--dry-run` does not require `claude`.
- A GitHub repo where the operator can push a branch and open a
  PR. The smoke uses a throwaway repo; do not run it against a
  repo under an active release freeze.
- A checked-out Turma repo with `uv sync` completed so
  `uv run turma` works.
- `jq` available for the verification commands below.

## Scratch setup

Point `TURMA_REPO` at your Turma checkout and run everything from
a scratch clone of a disposable GitHub repo so `bd`, `gh`, and
`turma run` all operate in one place:

```bash
export TURMA_REPO="$(cd ~/coding_projects/turma && pwd)"
test -x "$TURMA_REPO/.venv/bin/turma" || (cd "$TURMA_REPO" && uv sync)

# Replace with a disposable repo you control.
export SMOKE_REPO_URL="git@github.com:<you>/turma-run-smoke.git"

WORKDIR=$(mktemp -d)
cd "$WORKDIR"
git clone "$SMOKE_REPO_URL" .
git checkout -b main
git push -u origin main || true   # ensure `main` exists on origin

# Minimum Turma project layout: config + role prompts.
cp "$TURMA_REPO/turma.example.toml" turma.toml
mkdir -p .agents openspec/changes/smoke-run
cp "$TURMA_REPO/.agents/author.md" .agents/
cp "$TURMA_REPO/.agents/critic.md" .agents/

# Beads database (non-interactive skips bd init's wizard).
BD_NON_INTERACTIVE=1 bd init --prefix smoke

# Gitignore entries the orchestrator expects.
cat >> .gitignore <<'EOF'
.beads/*.db
.worktrees/
.task_complete
.task_failed
.task_progress
EOF
git add .gitignore turma.toml
git commit -m "smoke: turma project layout"
git push
```

## Pre-populate a transcribed feature

Pre-stage an approved + transcribed feature with one trivially
completable task. The smoke focuses on the orchestrator, not on
LLM-driven planning.

```bash
cat > openspec/changes/smoke-run/tasks.md <<'EOF'
## Tasks

### 1. Append a line to SMOKE.txt
- [ ] Create or append to SMOKE.txt with a single line of text.
EOF
printf '## Why\nStub.\n'   > openspec/changes/smoke-run/proposal.md
printf '## Goals\nStub.\n' > openspec/changes/smoke-run/design.md
touch openspec/changes/smoke-run/APPROVED
git add openspec/changes/smoke-run
git commit -m "smoke: approved spec"
git push

"$TURMA_REPO/.venv/bin/turma" plan-to-beads --feature smoke-run
git add openspec/changes/smoke-run/TRANSCRIBED.md
git commit -m "smoke: transcribed to Beads"
git push
```

`openspec/changes/smoke-run/TRANSCRIBED.md` now records the Beads
id. bd 1.0.2 uses `<prefix>-<hash>` ids, so you'll see something
like `smoke-vl1` rather than the older `bd-smoke-1` shape.
Capture it for reuse in later steps:

```bash
TASK_ID=$(bd list --label feature:smoke-run --status open --json --limit 0 \
            | jq -er '.[0].id')
echo "TASK_ID=$TASK_ID"
```

Confirm the task's labels and status with:

```bash
bd list --label feature:smoke-run --json --limit 0 \
  | jq '[.[] | {id, title, status}]'
```

## Step 1 — `--dry-run` surfaces reconciliation only

```bash
cd "$WORKDIR"
"$TURMA_REPO/.venv/bin/turma" run --feature smoke-run --dry-run
```

Expected stdout:

```
reconcile: 0 in-progress tasks
```

No Beads mutation, no worktree, no `gh pr create`. Verify with:

```bash
bd list --label feature:smoke-run --json --limit 0 \
  | jq '.[] | {id, status}'          # every task still `open`
git worktree list                     # only the main working copy
```

## Step 2 — Happy path, one task end-to-end

```bash
cd "$WORKDIR"
"$TURMA_REPO/.venv/bin/turma" run --feature smoke-run --max-tasks 1
```

Expected stdout shape (ids depend on bd's prefix; `<id>` below is
whatever `TASK_ID` holds from the capture above, e.g. `smoke-vl1`):

```
reconcile: 0 in-progress tasks
swarm: claimed <id> — Append a line to SMOKE.txt
swarm: opened <id> (PR: https://github.com/<you>/turma-run-smoke/pull/1; awaiting merge)
swarm: no ready tasks remain; done
```

Claude Code runs inside `.worktrees/smoke-run/<id>/`,
creates `SMOKE.txt`, writes `.task_complete`. The orchestrator
commits on branch `task/smoke-run/<id>`, pushes, opens a PR
against `main`, and labels the Beads task with `turma-pr:<N>`.
The task stays `in_progress` and the worktree stays on disk
until a future `turma run` observes the PR as merged
(see Step 3).

Verify:

```bash
bd show "$TASK_ID"                            # status: in_progress
                                              # labels include turma-pr:<N>
gh pr list --head "task/smoke-run/$TASK_ID" --state open \
  --json url,number                           # one entry
git branch -a | grep smoke-run
git worktree list                             # the per-task worktree should still be present
```

## Step 3 — Merge advancement closes the task on the next run

This step exercises the merge-advancement phase: when a `turma
run`-opened PR has been merged on GitHub, the next invocation
observes the merge, closes the Beads task, and removes the
worktree without the operator having to touch bd or the
worktree directly.

Merge the PR opened in Step 2:

```bash
PR_NUMBER=$(gh pr list --head "task/smoke-run/$TASK_ID" \
              --state open --json number --jq '.[0].number')
gh pr merge "$PR_NUMBER" --squash --delete-branch
```

Re-run the orchestrator:

```bash
cd "$WORKDIR"
"$TURMA_REPO/.venv/bin/turma" run --feature smoke-run
```

Expected: the merge-advancement phase fires before
`fetch_ready`, sees the PR as MERGED via
`gh pr view <N> --json state`, removes the `turma-pr:<N>`
label, closes the bd task, and cleans up the worktree.
With no further ready tasks (the smoke spec has only one
task), the main loop exits immediately.

```
reconcile: 0 in-progress tasks
merge-advancement: <id> → MERGED, closed
swarm: no ready tasks remain; done
```

Verify:

```bash
bd show "$TASK_ID"                            # status: closed
                                              # turma-pr:<N> label gone
git worktree list                             # the per-task worktree is gone
git branch --list "task/smoke-run/$TASK_ID"   # empty — branch was deleted on cleanup
```

If the smoke feature had a dependent task chained off this
one, that dependent would have become ready on this same
run and the main loop would claim it. The single-task smoke
exits early; an end-to-end test against a chain is the
quickest way to feel the dependency unblock in motion.

## Step 4 — Reconciliation on resume (`completion-pending`)

Simulate a crash between `run_worker` success and `commit`: the
worker wrote `.task_complete` into a per-task worktree but the
orchestrator never got to commit, push, or close the Beads task.

The critical detail: the orchestrator runs every git command as
`git -C <worktree> …` against a **registered** git worktree of the
parent repo. Reproducing the interrupted state therefore requires
`git worktree add -b …`, not `git init` inside the worktree path
(a nested standalone repo would let the orchestrator's commit step
succeed but `push` / `cleanup` would fail because the parent repo
has no record of it).

```bash
cd "$WORKDIR"

# Re-transcribe to get a fresh open task. --force closes the task
# that the merge-advancement sweep in Step 3 closed and creates a
# new one; capture the new id from bd directly rather than
# regex-parsing TRANSCRIBED.md, since bd 1.0.2 uses
# `<prefix>-<hash>` ids (e.g. smoke-vl1), not the older
# `bd-smoke-N` shape. `jq -e` fails loudly if the list is
# unexpectedly empty instead of silently returning `null`.
"$TURMA_REPO/.venv/bin/turma" plan-to-beads --feature smoke-run --force
NEW_ID=$(bd list --label feature:smoke-run --status open --json --limit 0 \
          | jq -er '.[0].id')

# Beads thinks a worker already claimed this task.
bd update "$NEW_ID" --claim

# Real registered worktree of the parent repo on a new task branch.
# Paths must match what WorktreeManager.worktree_path_for /
# branch_name_for return for (feature, task_id).
git worktree add -b "task/smoke-run/$NEW_ID" \
    ".worktrees/smoke-run/$NEW_ID" main

# Worker outputs — an edit git will pick up on `add -A`, plus the
# success sentinel the orchestrator's reconciliation walks for.
(cd ".worktrees/smoke-run/$NEW_ID" && \
   printf 'recovered\n' > SMOKE.txt && \
   touch .task_complete)
```

Run the orchestrator:

```bash
"$TURMA_REPO/.venv/bin/turma" run --feature smoke-run --max-tasks 1
```

Expected: reconciliation classifies `$NEW_ID` as
`completion-pending` (`.task_complete` present, no open PR
yet), the repair phase runs `commit + push + open_pr +
mark_pr_open` against the registered worktree (close + cleanup
defer to merge advancement), then the main loop finds nothing
ready and exits.

```
reconcile: 1 in-progress task
reconcile:   <NEW_ID> → completion-pending
repair: <NEW_ID> → committed, pushed, PR opened (...; awaiting merge), labelled
swarm: no ready tasks remain; done
```

Verify:

```bash
bd show "$NEW_ID" | head                         # status: in_progress
                                                 # labels include turma-pr:<N>
gh pr list --head "task/smoke-run/$NEW_ID" \
  --state open --json url,number                  # one entry
git worktree list                                 # per-task worktree still present
git branch --list "task/smoke-run/$NEW_ID"        # branch still present
```

(Merging this PR + re-running `turma run` would advance the
task through merge-advancement just like Step 3.)

## Step 5 — Failure path surfaces on `fail_task`

Pre-populate another task that the worker will deliberately mark
as failed:

```bash
cd "$WORKDIR"
cat > openspec/changes/smoke-run/tasks.md <<'EOF'
## Tasks

### 1. Fail on purpose
- [ ] Write the reason to .task_failed and stop.
EOF
git add openspec/changes/smoke-run/tasks.md
git commit -m "smoke: fail-path task"
git push

"$TURMA_REPO/.venv/bin/turma" plan-to-beads --feature smoke-run --force
FAIL_ID=$(bd list --label feature:smoke-run --status open --json --limit 0 \
           | jq -er '.[0].id')
```

Run — the worker prompt tells Claude Code how to signal failure,
so the failure path fires naturally. With `max_retries = 1` (the
default) and no prior retries, the task goes back to `open` rather
than being labelled `needs_human_review`; a second failed run
exhausts the budget.

```bash
"$TURMA_REPO/.venv/bin/turma" run --feature smoke-run --max-tasks 1
# swarm: claimed $FAIL_ID — Fail on purpose
# swarm: $FAIL_ID failed (attempt 1/2): <reason>
# swarm: no ready tasks remain; done

"$TURMA_REPO/.venv/bin/turma" run --feature smoke-run --max-tasks 1
# swarm: claimed $FAIL_ID — Fail on purpose
# swarm: $FAIL_ID failed (budget exhausted after 2 attempts): <reason>
# error: retry budget exhausted on $FAIL_ID; halting run. Triage
# with `bd show $FAIL_ID` and `bd list --label needs_human_review`.
```

Exit status is 1 on the second run. Confirm the labels:

```bash
bd list --label needs_human_review --json --limit 0 \
  | jq '.[] | {id, title, labels}'
ls ".worktrees/smoke-run/$FAIL_ID"   # worktree is preserved for triage
```

## Cleanup

```bash
# Close the smoke's feature-tagged tasks and remove the worktrees.
bd list --label feature:smoke-run --json --limit 0 \
  | jq -r '.[].id' | xargs -r bd close

for wt in .worktrees/smoke-run/*/; do
  branch=$(basename "$wt")
  git worktree remove --force "$wt" 2>/dev/null || rm -rf "$wt"
  git branch -D "task/smoke-run/$branch" 2>/dev/null || true
done

rm -rf "$WORKDIR"
```

On GitHub, close or delete the smoke PRs and delete the
`task/smoke-run/*` remote branches if the repo's settings did not
auto-delete them on close.

## Failure-signature cheat sheet

- `error: feature 'X' is not APPROVED` — no `APPROVED` marker;
  run `turma plan` first.
- `error: feature 'X' has not been transcribed to Beads` — no
  `TRANSCRIBED.md`; run `turma plan-to-beads --feature X`.
- `error: bd CLI not found. Install it with \`brew install beads\`` —
  `bd` missing from PATH in the shell that invoked Turma.
- `error: gh CLI not found` — `gh` missing from PATH.
- `error: gh session not authenticated. Run \`gh auth login\` and retry.` —
  run `gh auth login` once; the adapter runs `gh auth status` at
  construction.
- `error: claude CLI not found` — `claude` missing from PATH;
  only fires when a task is actually claimed, not during
  `--dry-run`.
- `error: stale worktree for <id> has no sentinels` — reconcile
  caught ambiguous state; inspect
  `.worktrees/<feature>/<id>/` and decide. The orchestrator
  never guesses.
- `error: retry budget exhausted on <id>` — task hit
  `max_retries`; triage with
  `bd list --label needs_human_review`. The failed worktree
  stays on disk as the primary diagnostic artifact.
- `error: gh pr create failed: ... Resource not accessible by
  personal access token` — the authenticated `gh` session lacks
  `pull_requests:write` scope. Fix under Settings → Personal
  access tokens for the repo owner.

## Notes for future work

- `--max-tasks` counts successfully-claimed tasks, not claim
  attempts. Claim races do not consume budget.
- Reconciliation is read-only and runs every invocation,
  including `--dry-run`. The only network call it issues is a
  `gh pr list` per task with `.task_complete`, to disambiguate
  `completion-pending` from `completion-pending-with-pr`.
- Failed worktrees are never auto-removed in v1. Manual cleanup:
  `git worktree remove --force <path>` + `git branch -D
  task/<feature>/<id>`. A `turma run --clean <feature>` flag is
  deferred past v1 (see Open items in `openspec/changes/
  swarm-orchestration/design.md`).
