# AGENTS

This file is the canonical repo-wide guide for agentic and human contributors.
It defines the working contract for changes in this repository. Role-specific
guidance lives under `.agents/`.

## Purpose

Turma is a provider-pool-aware multi-agent coding orchestrator. The repo
carries a working CLI surface (`init`, `plan`, `plan-to-beads`, `run`,
`status`) over an implemented planning + swarm engine, alongside the design
documentation. The long-term workflow is:

1. plan a feature through explicit spec authoring and critique
2. translate approved work into executable task units
3. implement one task per isolated worktree
4. treat integration as the actual completion boundary

## Authoritative Sources

When guidance conflicts, prefer these sources in order:

1. `docs/architecture.md`
2. `AGENTS.md`
3. role guidance in `.agents/*.md`
4. inline comments and local implementation details

## Tracked vs Local State

Commit:

- `src/`
- `tests/`
- `docs/`
- `.agents/`
- `.claude/commands/`
- `turma.example.toml`

Do not commit:

- `turma.toml`
- `.env*`
- `.langgraph/`
- `.turma/state/`
- `.claude/settings.local.json`
- `.claude/todos/`
- `.codex/`
- ad hoc logs, task progress markers, or local runtime databases

## Configuration Rule

- `turma.example.toml` is the tracked template
- each contributor copies it to local `turma.toml`
- local provider settings, paths, and concurrency overrides stay untracked

## OpenSpec Workflow

OpenSpec is part of this repo's intended feature workflow, but it does not
replace `AGENTS.md` as the repo-wide contract.

Use OpenSpec for feature changes that are mature enough to enter the real spec
and implementation loop.

Practical rules:

- feature specs live under `openspec/changes/`
- rough ideation should be stabilized before it becomes an OpenSpec change
- implementation should follow approved specs rather than ad hoc chat history
- OpenSpec tool integrations may be repo-local for some tools and global for
  others, but the source of truth for feature artifacts is the repo

## Change Scope

Use this rule for implementation work:

- one task = one PR = one logical concern

If a task requires unrelated changes across multiple subsystems, the task was
scoped incorrectly and should be split earlier in planning.

"Small and task-bounded" means:

- the change has one clear reason to exist
- the diff can be reviewed against one acceptance criterion set
- follow-up work is represented as new tasks, not hidden expansion

## Task Completion Semantics

Authored code is not the same as completed work.

For this repo, treat task completion as:

- implementation exists
- validation passes at the task level
- review/integration state is known
- the change is ready to merge or already merged, depending on workflow mode

Do not treat "PR opened" as equivalent to "task done."

## Working Workflow

For now, use this practical contributor flow:

1. read the relevant OpenSpec change, design, and task context first
2. make the smallest coherent change that satisfies the task
3. validate locally with the project-standard commands
4. update docs/config/examples if the public contract changed
5. keep history legible by separating unrelated concerns into separate commits

## Post-Merge Cleanup

After a PR merges, sync `main` and delete the merged branch — but do not rely
on `git branch --merged` to decide what is merged.

This repo merges PRs by squash/rebase, which lands a new commit on `main` with
a different SHA than the branch tip. The original tip is therefore not an
ancestor of `main`, so `git branch --merged` reports already-merged branches as
unmerged. Build a candidate list from the GitHub merged-PR list instead:

```bash
# branch names of merged PRs
gh pr list --state merged --limit 300 --json headRefName --jq '.[].headRefName' \
  | sort -u > /tmp/merged_prs.txt

# local branches, excluding main
git for-each-ref --format='%(refname:short)' refs/heads/ \
  | grep -vE '^(main)$' | sort -u > /tmp/local_branches.txt

# candidates: local branches whose name matches a merged PR
comm -12 /tmp/local_branches.txt /tmp/merged_prs.txt > /tmp/branches_to_delete.txt
```

A name match is a *candidate*, not proof a branch is disposable — a branch can
share a name with an old merged PR yet still hold new, unmerged work. So treat
`/tmp/branches_to_delete.txt` as a starting point, not a delete queue. Before
deleting anything, open the file and **edit it down to only the branches you
have confirmed are disposable**:

- inspect each candidate
- delete the line for any long-lived or local-only branch you intend to keep
- verify the merge against the real ref (`origin/main`), not a summary

Only then run the delete, and have it consume the *reviewed file* — never
re-derive the raw candidate set at delete time:

```bash
# operates on the hand-reviewed file, not a fresh comm of all candidates
xargs -r -n1 git branch -D < /tmp/branches_to_delete.txt
```

`git branch -D` (force) is expected here because the squash rewrote the SHA, so
plain `git branch -d` would refuse a genuinely-merged branch — run it only over
branches you have already confirmed are disposable.

## Testing Discipline

This repo expects test-first work when behavior is testable.

Practical rules:

- write the failing test first for behavior changes when the behavior can be
  exercised in automation
- pin each acceptance criterion with at least one test, or explicitly mark it
  as manual-only / operator-verified
- do not treat "tests added later" as equivalent to test-first unless there is
  a concrete reason the test had to wait
- if a behavior change ships without automated coverage, explain the gap in the
  PR description and in review

This is a repo policy and review standard, not a mechanical git restriction.
Branch protection, CI, and review enforce it in practice; `AGENTS.md` defines
the expected contributor behavior.

Current validation baseline:

```bash
uv sync
uv run turma init
uv run turma --help
uv run python -m turma --help
uv run pytest
```

Current implementation note:

- `turma init`, `turma plan`, `turma plan-to-beads`, `turma run`, and
  `turma status` are all implemented over the real engine and covered by tests
- `turma plan` runs the full author/critic loop with a human approval gate and
  a resume CLI; `turma run` drives the swarm against the transcribed Beads DAG;
  `turma status` is a read-only, no-mutation feature readout
- remaining operator-surface work is experience polish (progress output,
  machine-readable modes), not net-new commands

## Agent Roles

Use `.agents/` for role-specific behavior:

- `.agents/author.md`
- `.agents/critic.md`
- `.agents/implementer.md`
- `.agents/reviewer.md`

If you are acting in one of those roles, follow `AGENTS.md` first and then the
role file.

## Notes For Future Contributors

- do not document aspirational workflow as if it already exists in code
- keep design-doc changes and implementation changes logically separated when possible
- if reconciliation semantics, task state, or completion semantics change, update
  `docs/architecture.md` in the same change
