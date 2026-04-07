# Tasks: OpenCode backend for turma plan

## Task 1: Implement OpenCode author backend

Type: impl
Priority: 1

Create `src/turma/authoring/opencode.py`:

- [ ] `OpenCodeAuthorBackend` implements `AuthorBackend`
- [ ] `__init__` validates `opencode` is on PATH via `shutil.which`
- [ ] `generate` runs `opencode run --model <model> <prompt>` as subprocess
- [ ] Uses `capture_output=True, text=True` for clean stdout capture
- [ ] Handles `subprocess.TimeoutExpired` → `PlanningError`
- [ ] Handles non-zero exit → `PlanningError` via `extract_process_error`
- [ ] Returns `result.stdout` on success

## Task 2: Write OpenCode backend tests

Type: test
Priority: 1

Create `tests/test_authoring_opencode.py`:

- [ ] Success: returns stdout on zero exit
- [ ] Failure: surfaces stderr on non-zero exit
- [ ] Failure: falls back to stdout when stderr is empty
- [ ] Timeout: raises PlanningError with duration
- [ ] Init: raises PlanningError when opencode CLI not on PATH
- [ ] Verifies exact subprocess command structure: `["opencode", "run", "--model", model, prompt]` — the `run` subcommand is load-bearing

## Task 3: Update backend routing

Type: impl
Priority: 1
Blocked by: Task 1

Update `_get_backend` in `src/turma/planning.py`:

- [ ] Add `"/" in model` check before existing prefix checks
- [ ] Route to `OpenCodeAuthorBackend`

## Task 4: Write backend routing tests

Type: test
Priority: 1
Blocked by: Task 3

Update `tests/test_planning.py`:

- [ ] `groq/llama-3.3-70b-versatile` selects OpenCode backend
- [ ] `anthropic/claude-sonnet-4-6` selects OpenCode backend (provider/model format)
- [ ] `claude-opus-4-6` still selects Claude backend (unchanged)
- [ ] `gpt-5.4` still selects Codex backend (unchanged)
- [ ] Unknown model without `/` still fails (unchanged)

## Task 5: Update config example

Type: docs
Priority: 2
Blocked by: Task 3

Update `turma.example.toml`:

- [ ] Add commented OpenCode example under `[planning]`

## Task 6: Verify CI

Type: test
Priority: 2
Blocked by: Task 2, Task 4

- [ ] All tests pass locally
- [ ] CI green on push

## Task 7: Run a real OpenCode smoke test

Type: test
Priority: 2
Blocked by: Task 1, Task 3

- [ ] Set `planning.author_model` in local `turma.toml` to an OpenCode-compatible model such as `groq/llama-3.3-70b-versatile`
- [ ] Ensure the required provider credential (for example `GROQ_API_KEY`) is present in the environment
- [ ] Run `turma plan --feature smoke-turma-plan-opencode`
- [ ] Verify `proposal.md`, `design.md`, and `tasks.md` are generated under the new change directory
- [ ] Verify artifact content is clean and does not include OpenCode tool/status noise
- [ ] If artifact stdout is polluted, switch the backend design to `--format json` and parse assistant text events instead of shipping the text-mode assumption
- [ ] Remove the disposable smoke-test change directory after verification

## Task 8: Update public status docs

Type: docs
Priority: 3
Blocked by: Task 7

- [ ] Update `README.md` to mention OpenCode as a supported `turma plan` backend
- [ ] Update `docs/index.html` and `docs/architecture.md` only if the public capability summary changes materially
- [ ] Update `CHANGELOG.md` if the change is intended for the next public release

## Task 9: Remove branch-only handoff artifacts before PR

Type: chore
Priority: 3
Blocked by: Task 7

- [ ] Remove `openspec/changes/turma-plan-opencode/IMPLEMENTATION_DONE.md` from branch history before pushing or opening a PR
