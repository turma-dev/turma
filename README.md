# Turma

Provider-pool-aware multi-agent coding orchestration with spec-driven planning,
Beads task tracking, and resumable swarm execution.

## Status

Early implementation phase. This repo now has the Python package layout,
OpenSpec workflow scaffolding, a working `turma init` command, baseline CI, and
public architecture documentation. The full orchestrator described in the
architecture docs is not implemented yet.

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
- `turma plan`, `turma run`, and `turma status` are still scaffolds

`turma init` expects `turma.example.toml` to exist in the target directory. It
creates `turma.toml` from that template and updates `.gitignore` with
Turma-managed entries.

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

- wire `turma plan` to the planning graph and OpenSpec artifacts
- wire `turma run` to Beads plus worktree orchestration
- persist reconciliation metadata for resumable task recovery
- replace placeholder status output with task, PR, and CI state

## License

MIT
