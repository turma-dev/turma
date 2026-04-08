# Turma

Provider-pool-aware multi-agent coding orchestration with spec-driven planning,
Beads task tracking, and resumable swarm execution.

## Status

Early implementation phase. This repo now has the Python package layout,
OpenSpec workflow scaffolding, a working `turma init` command, a working
single-pass `turma plan` command, baseline CI, and public architecture
documentation. The full author/critic planning loop and execution orchestrator
described in the architecture docs are not implemented yet.

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
- `turma plan` is implemented as a single-pass author workflow
- `turma run` and `turma status` are still scaffolds

`turma init` expects `turma.example.toml` to exist in the target directory. It
creates `turma.toml` from that template and updates `.gitignore` with
Turma-managed entries.

`turma plan --feature <name>` currently:

- reads `planning.author_model` from `turma.toml`
- requires `.agents/author.md`
- scaffolds an OpenSpec change with `openspec`
- generates `proposal`, `design`, and `tasks` in a fixed order
- supports Claude, Codex, Gemini, and OpenCode-backed author generation

Planning quality depends on the chosen backend/model. Claude-backed planning is
currently the strongest validated path. OpenCode transport is validated, but
provider/model quality varies. Gemini requires the `gemini` CLI
(`npm install -g @google/gemini-cli`).

It does not yet run a critic loop, commit changes, or orchestrate execution.

Validation commands:

```bash
uv run turma --help
uv run python -m turma --help
uv run pytest
```

## Core Docs

- [Architecture](docs/architecture.md)
- [Changelog](CHANGELOG.md)

## Next Implementation Steps

- add the critic loop and approval flow on top of `turma plan`
- wire `turma run` to Beads plus worktree orchestration
- persist reconciliation metadata for resumable task recovery
- replace placeholder status output with task, PR, and CI state

## License

MIT
