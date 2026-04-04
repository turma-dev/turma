# Plan to Epic

Convert an OpenSpec `tasks.md` document into a Beads epic with task types,
priorities, and dependencies.

## Usage

`/plan-to-epic <path-to-tasks.md> [--design <path-to-design.md>]`

## Workflow

1. Read the `tasks.md` file at the provided path.
2. If `--design` is provided, read `design.md` for architecture context.
3. Identify atomic tasks, task types, priorities, and dependency edges.
4. Create the Beads epic and child tasks.
5. Report the resulting graph and call out any ambiguity that still requires human review.

## Rules

- Do not start implementation work.
- Do not mark tasks complete.
- Treat task completion as integrated work, not merely authored code.
- Flag dependencies that appear to rely on merge order or CI state.

