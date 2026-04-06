# Tasks: turma plan (single-pass author)

## Task 1: Implement config loading

Type: impl
Priority: 1

Create `src/turma/config.py`:

- [ ] `load_config()` reads `turma.toml` from cwd using `tomllib`
- [ ] Returns dataclass with `planning.author_model` and other planning fields
- [ ] Fails clearly when `turma.toml` not found
- [ ] Fails clearly when `planning.author_model` missing
- [ ] Fails on malformed TOML
- [ ] Ignores unknown sections gracefully

## Task 2: Write config tests

Type: test
Priority: 1

Create `tests/test_config.py`:

- [ ] Loads valid config with correct values
- [ ] Fails when turma.toml missing
- [ ] Fails when planning.author_model missing
- [ ] Fails on malformed TOML
- [ ] Ignores unknown sections

## Task 3: Implement turma plan orchestration

Type: impl
Priority: 1
Blocked by: Task 1

Replace stub in `src/turma/planning.py`:

- [ ] Loads config
- [ ] Validates .agents/author.md exists
- [ ] Validates openspec and claude on PATH
- [ ] Fails if change directory already exists
- [ ] Runs openspec new change
- [ ] Gets instructions via openspec instructions --json
- [ ] Assembles author prompt with role + instructions + dependencies
- [ ] Runs claude -p per artifact in fixed order (proposal, design, tasks)
- [ ] Writes output to outputPath from instructions JSON
- [ ] Reports progress
- [ ] Prints stderr and returns 1 on subprocess failure

## Task 4: Write planning tests

Type: test
Priority: 1
Blocked by: Task 3

Create `tests/test_planning.py` with subprocess mocking:

- [ ] Fails when turma.toml missing
- [ ] Fails when .agents/author.md missing
- [ ] Fails when openspec not on PATH
- [ ] Fails when claude not on PATH
- [ ] Fails when change directory already exists
- [ ] Calls openspec new change correctly
- [ ] Uses outputPath from openspec instructions JSON
- [ ] Generates artifacts in fixed order: proposal, design, tasks
- [ ] Prompt includes author.md content
- [ ] Prompt includes openspec instructions
- [ ] Design prompt includes proposal as dependency
- [ ] Tasks prompt includes proposal + design as dependencies
- [ ] Writes claude stdout to correct outputPath
- [ ] Non-zero subprocess exit prints stderr, returns 1

## Task 5: Update CLI wiring

Type: impl
Priority: 1
Blocked by: Task 3

Update `src/turma/cli.py`:

- [ ] Extract cmd_plan with proper error handling and exit codes
- [ ] Update main() dispatch

## Task 6: Verify CI

Type: test
Priority: 2
Blocked by: Task 4, Task 5

- [ ] All tests pass locally
- [ ] CI green on push
