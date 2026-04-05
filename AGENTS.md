# AGENTS

This file is the canonical repo-wide guide for agentic and human contributors.
It defines the working contract for changes in this repository. Role-specific
guidance lives under `.agents/`.

## Purpose

Turma is a provider-pool-aware multi-agent coding orchestrator. The repo is
currently an early Python scaffold plus design documentation. The long-term
workflow is:

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

1. read the relevant design and task context first
2. make the smallest coherent change that satisfies the task
3. validate locally with the project-standard commands
4. update docs/config/examples if the public contract changed
5. keep history legible by separating unrelated concerns into separate commits

Current validation baseline:

```bash
uv sync
cp turma.example.toml turma.toml
uv run turma --help
uv run python -m turma --help
uv run pytest
```

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
