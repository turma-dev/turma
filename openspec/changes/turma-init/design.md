# Design: turma init

## Goals

`turma init` prepares a project directory for local Turma usage.

It does the following:

1. **Copy config template.** Copy `turma.example.toml` to `turma.toml` in the
   target directory. The example template is the single canonical config source.

2. **Update .gitignore.** Ensure `.gitignore` contains Turma-managed entries.
   If `.gitignore` does not exist, create it. If it exists, append only the
   entries that are not already present. Preserve existing content and ordering.

3. **Report actions.** Print a human-readable summary of what was created and
   what was skipped.

4. **Idempotent.** Safe to re-run. If `turma.toml` already exists, skip it
   (unless `--force` is passed). If `.gitignore` already contains all required
   entries, skip it.

## Arguments

- `--path <dir>` — project directory to initialize. Defaults to `.`.
- `--force` — overwrite existing `turma.toml` even if it already exists.

## Config source

One canonical source: `turma.example.toml` in the target directory. If it does
not exist, the command exits with a non-zero status and a clear error message.
There is no built-in fallback template shipped in the package.

## .gitignore entries

The following lines are managed by `turma init`:

```
# Turma local state
turma.toml
.turma/
.langgraph/
*.task_complete
*.task_progress
```

These are appended as a block with a leading comment. If any individual line
already exists anywhere in the file, that line is not duplicated.

## Output

- Human-readable summary only.
- No machine-readable or structured output mode.
- Example output when creating files:
  ```
  created turma.toml from turma.example.toml
  updated .gitignore (added 5 entries)
  ```
- Example output when everything exists:
  ```
  skipped turma.toml (already exists, use --force to overwrite)
  skipped .gitignore (all entries present)
  ```

## Exit codes

- `0` — success (including idempotent no-op).
- `1` — failure (missing `turma.example.toml` or write error).

## Non-goals

`turma init` does NOT:

- Install Python or system dependencies.
- Create `openspec/` directory (that is `openspec init`).
- Create `.agents/` or `.claude/commands/` (those are repo-setup concerns).
- Create runtime state directories (`.turma/state/`, `.langgraph/`). Those are
  created lazily by the commands that need them.
- Modify `pyproject.toml` or other project metadata.
- Configure provider credentials or API keys.
- Run any network requests.
- Ship a built-in default config template. The example file must exist.
