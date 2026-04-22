# Turma

Provider-pool-aware multi-agent coding orchestration with spec-driven planning,
Beads task tracking, and resumable swarm execution.

## Status

Early implementation phase. This repo has the Python package layout, OpenSpec
workflow scaffolding, a working `turma init` command, a working `turma plan`
command running a full author/critic loop with an explicit human approval
gate and resume CLI, baseline CI, and public architecture documentation. The
execution orchestrator described in the architecture docs is not implemented
yet.

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
- `turma run` and `turma status` are still scaffolds

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

- wire `turma run` to Beads plus worktree orchestration
- persist reconciliation metadata for resumable task recovery
- replace placeholder status output with task, PR, and CI state

## License

MIT
