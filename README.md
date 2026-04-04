# Turma

Provider-pool-aware multi-agent coding orchestration with spec-driven planning,
Beads task tracking, and resumable swarm execution.

## Status

Early scaffold. This repo now has the initial Python package layout, corrected
design docs, baseline CLI entry point, and project configuration. The full
orchestrator described in the architecture docs is not implemented yet.

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
├── .agents/                    # role guidance for author / critic / implementer / reviewer
├── .claude/commands/           # slash commands used in project context
├── docs/
│   ├── architecture.md         # detailed system design
├── src/turma/                 # Python package scaffold
├── tests/                      # initial test scaffold
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
cp turma.example.toml turma.toml
uv run turma --help
uv run turma init
uv run turma plan --feature oauth-auth
uv run turma run --feature oauth-auth
uv run turma status
```

These commands are scaffolds today. They provide the initial package and entry
point structure that the orchestrator can grow into.

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

- wire `turma plan` to the planning graph
- wire `turma run` to Beads plus worktree orchestration
- persist reconciliation metadata for resumable task recovery
- replace placeholder status output with task, PR, and CI state

## License

MIT
