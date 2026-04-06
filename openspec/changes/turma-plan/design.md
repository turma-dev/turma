# Design: turma plan (single-pass author)

## Goals

`turma plan --feature <name>` produces OpenSpec artifacts for a feature using a
single author pass via `claude -p`.

It does the following:

1. **Load config.** Read `turma.toml` from cwd. Require `planning.author_model`.
   Fail clearly if the file is missing or the key is absent.

2. **Validate prerequisites.** Check that `.agents/author.md`, `openspec` CLI,
   and `claude` CLI all exist. Fail clearly for each if missing.

3. **Fail if change exists.** If `openspec/changes/<feature>/` already exists,
   exit with a non-zero status. No resume, no skip.

4. **Scaffold change.** Run `openspec new change "<feature>"` to create the
   change directory.

5. **Generate artifacts in fixed order.** For each of `proposal`, `design`,
   `tasks`:
   - Get instructions via `openspec instructions <id> --change <feature> --json`
   - Read `.agents/author.md` for role context
   - Read completed dependency artifacts (using `outputPath` from prior steps)
   - Assemble prompt and run `claude -p "<prompt>" --model <author_model>`
   - Write stdout to the `outputPath` from the instructions JSON

6. **Report progress.** Print which artifacts were generated and their paths.

## Arguments

- `--feature <name>` — required. The OpenSpec change name.

## Config

Read from `turma.toml` in the current working directory only. No `--path`
argument in v1.

Required key: `planning.author_model`. If missing, fail with a clear error.

No silent code-level defaults for model names. `turma.example.toml` documents
the recommended values; code reads what the user's config provides.

## Source of truth

OpenSpec instructions JSON is the source of truth for artifact metadata. Turma
reads `outputPath`, `template`, `instruction`, `dependencies`, `context`, and
`rules` from the JSON output of `openspec instructions`. Turma does not
re-derive artifact graph logic or assume filenames.

`outputPath` from OpenSpec is interpreted relative to the change directory
(`openspec/changes/<feature>/`), not relative to the project root.

## Supported artifacts (v1)

Fixed order: `proposal`, `design`, `tasks`. This is not a generic artifact-graph
executor. v1 processes exactly these three in this order.

## Prompt assembly

For each artifact, the prompt is assembled from:
- `.agents/author.md` content (role definition)
- `instruction` field from openspec instructions JSON
- `template` field from openspec instructions JSON
- `context` and `rules` fields from openspec instructions JSON (as constraints)
- Content of completed dependency artifacts

Directive: output only the artifact markdown content, no preamble or code fences.

## Subprocess failure behavior

- Non-zero exit from `openspec` or `claude` → print stderr, return exit code 1
- Partial artifacts are not cleaned up
- The error message makes clear which step failed
- No retry or resume logic

## Output

Human-readable progress only. No machine-readable mode.

Example:
```
loading config from turma.toml
creating change: oauth-auth
generating proposal.md ... done
generating design.md ... done
generating tasks.md ... done

planning complete. artifacts written to openspec/changes/oauth-auth/
```

## Exit codes

- `0` — success
- `1` — failure (missing config, missing prerequisites, existing change,
  subprocess failure)

## Non-goals

`turma plan` v1 does NOT:

- Run a critic loop or use `critic_model`
- Use LangGraph or checkpointing
- Require Beads
- Git commit, branch, or create PRs
- Prompt the user interactively
- Retry or resume on failure
- Generate `specs/` artifacts
- Clean up partial artifacts on failure
- Accept a `--path` argument
- Default `author_model` silently in code
