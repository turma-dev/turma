# Tasks: turma init

## Task 1: Write tests for turma init

Type: test
Priority: 1

Create `tests/test_init.py` with tests derived from the design spec:

- [x] Creates `turma.toml` from `turma.example.toml` in a temp directory
- [x] Skips `turma.toml` if it already exists (no `--force`)
- [x] Overwrites `turma.toml` when `--force` is passed
- [x] Creates `.gitignore` with turma entries if file does not exist
- [x] Appends missing turma entries to existing `.gitignore`
- [x] Does not duplicate entries already in `.gitignore`
- [x] Preserves existing `.gitignore` content and order
- [x] Exits non-zero with clear error when `turma.example.toml` is missing
- [x] Idempotent — second run reports skipped, changes nothing
- [x] Reports created vs skipped items in output

Start with the first test case only. Expand after CI is set up.

## Task 2: Implement minimum turma init to pass first test

Type: impl
Priority: 1
Blocked by: Task 1 (first test case only)

Replace the `cmd_init` stub in `src/turma/cli.py` with the minimum code to
copy `turma.example.toml` to `turma.toml`. Extract to `src/turma/init.py` if
the logic exceeds ~30 lines.

Acceptance criteria: first test passes locally. Completed.

## Task 3: Add GitHub Actions CI workflow

Type: impl
Priority: 1
Blocked by: Task 2

Create `.github/workflows/ci.yml` that runs:
- `uv sync`
- `uv run turma --help`
- `uv run pytest`

Push and verify CI is green. CI must be green from its first run.

Status: completed.

## Task 4: Expand tests to cover full spec

Type: test
Priority: 2
Blocked by: Task 3

Add remaining test cases from Task 1 checklist. Run locally and verify CI
stays green after each addition.

Status: completed.

## Task 5: Implement remaining turma init behavior

Type: impl
Priority: 2
Blocked by: Task 4

Implement:
- `.gitignore` creation and append logic
- `--force` flag
- Error handling for missing template
- Human-readable output reporting

Acceptance criteria: all tests pass locally and in CI.

Status: completed.

## Task 6: Update README if prerequisites changed

Type: docs
Priority: 3
Blocked by: Task 5

Review whether the public README needs updates based on what `turma init`
now requires or produces. Only update if something user-facing changed.

Status: completed.
