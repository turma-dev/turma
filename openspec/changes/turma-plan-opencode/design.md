# Design: OpenCode backend for turma plan

## Goals

Add `OpenCodeAuthorBackend` so that `turma plan --feature <name>` can generate
spec artifacts using OpenCode backed by Groq or any other provider OpenCode
supports.

## OpenCode CLI invocation

OpenCode's non-interactive mode is `opencode run`. Key behaviors confirmed from
source (`/packages/opencode/src/cli/cmd/run.ts`):

- Prompt is a positional argument: `opencode run "your prompt"`
- Model uses `provider/model` format: `--model groq/llama-3.3-70b-versatile`
- When stdout is piped (non-TTY), text output goes to stdout cleanly
- Tool activity and status go to stderr via the `UI` module
- Permission prompts are auto-denied in `run` mode — no interactive hang
- No `-p` flag exists; non-interactive output is automatic when piped
- `--format json` provides structured newline-delimited JSON events

### Recommended subprocess invocation

```python
["opencode", "run",
 "--model", model,       # e.g. "groq/llama-3.3-70b-versatile"
 prompt]                 # positional argument
```

With `capture_output=True`, stdout is piped (non-TTY), so OpenCode is expected
to write only the text response to stdout while routing UI/tool activity to
stderr. This is acceptable for v1 only if the real smoke test confirms that the
captured artifact output is clean.

`--format json` is not the default design for v1 because the simpler text path
should be easier to implement and review. However, this is a provisional choice,
not a guaranteed contract. If the real smoke test shows tool/status noise or any
other stdout pollution, the implementation must switch to `--format json` and
extract only assistant text events before the feature is considered complete.

So the v1 rule is:

- start with default piped text capture
- keep smoke-testing as the gate
- fall back to structured JSON parsing if clean stdout does not hold in practice

No `--dir` needed — the subprocess inherits cwd from Turma.

### Authentication

OpenCode auto-discovers provider credentials from environment variables.
For Groq: `GROQ_API_KEY` must be set in the environment. OpenCode also
supports stored credentials via `opencode providers login --provider groq`.

The backend does not manage credentials. If auth is missing, OpenCode will
fail with a non-zero exit and a diagnostic message in stderr.

### Bootstrap cost

Each `opencode run` invocation initializes a SQLite database and loads
providers. This adds ~1-2 seconds of startup overhead per call. For three
sequential artifact generations, this is acceptable. The `--attach` server
mode exists for amortizing this cost but is out of scope for v1.

## Backend implementation

### `src/turma/authoring/opencode.py`

```python
class OpenCodeAuthorBackend(AuthorBackend):
    def __init__(self) -> None:
        # Validate opencode is on PATH
    
    def generate(self, prompt: str, model: str, timeout: int) -> str:
        # subprocess.run(["opencode", "run", "--model", model, prompt], ...)
        # capture_output=True, text=True, timeout=timeout
        # On success: return result.stdout
        # On failure: raise PlanningError with extract_process_error
        # On timeout: raise PlanningError
```

The pattern follows `ClaudeAuthorBackend` exactly:
- `__init__` validates CLI presence via `shutil.which("opencode")`
- `generate` runs the subprocess, handles timeout and non-zero exit
- Error extraction uses the shared `extract_process_error` helper
- Returns `result.stdout` on success

Note: `opencode` (the agent runtime) and `openspec` (the spec framework) are
separate tools with separate PATH checks. `planning.py` already validates
`openspec` at step 3; the OpenCode backend validates `opencode` at init.
Error messages must name the correct tool so users don't confuse them.

### Model format and backend routing

OpenCode models use `provider/model` format (e.g. `groq/llama-3.3-70b-versatile`).
This does not collide with existing backend routing:

- `claude-*` → Claude backend
- `gpt-*`, `codex-*`, `o*` → Codex backend
- Contains `/` → OpenCode backend (new)

The `/` character is the distinguishing signal — neither Claude Code CLI
nor Codex CLI model names contain a slash. This is the same convention
OpenCode itself uses internally.

This means `anthropic/claude-sonnet-4-6` routes to the OpenCode backend,
not the Claude Code CLI backend. This is intentional: when a user writes a
`provider/model` string, they are explicitly choosing to invoke that model
through OpenCode's provider system rather than through the provider's own
CLI. If a user wants the Claude Code CLI, they use `claude-opus-4-6`
(no slash).

Update `_get_backend` in `planning.py`:

```python
if "/" in model:
    return OpenCodeAuthorBackend()
```

This should be checked before the Claude and Codex prefix checks.

### Config example

Add to `turma.example.toml` as a commented alternative:

```toml
[planning]
author_model = "claude-opus-4-6"
# author_model = "groq/llama-3.3-70b-versatile"   # OpenCode + Groq
```

No new config keys. The existing `author_model` field is sufficient.

## What does NOT change

- `src/turma/planning.py` orchestration logic (except `_get_backend` routing)
- `src/turma/authoring/base.py`
- `src/turma/authoring/claude.py`
- `src/turma/authoring/codex.py`
- `src/turma/config.py`
- `src/turma/cli.py`
- Prompt assembly
- Artifact validation
- OpenSpec interaction
- Dependency chaining
- Exit codes and error reporting contract

## Smoke test expectations

A successful smoke test should:

1. Set `author_model = "groq/llama-3.3-70b-versatile"` in `turma.toml`
2. Ensure `GROQ_API_KEY` is set in the environment
3. Run `turma plan --feature smoke-opencode`
4. Observe three artifacts generated faster than Claude Opus (~10-30s each
   vs ~100s)
5. Verify artifacts contain the expected template headings
6. Verify no OpenCode tool noise in the artifact content (stderr is
   captured separately)

A failed smoke test should surface:
- Missing `GROQ_API_KEY` → OpenCode exits non-zero with auth error
- Missing `opencode` CLI → PlanningError at backend init
- Timeout → PlanningError with duration

This smoke test is not optional for merge readiness. The feature depends on
source-code analysis today, but the implementation must be validated against a
real `opencode run` invocation before it is treated as complete.

If the smoke test reveals stdout contamination from tool/status output, the
feature is not ready as designed. In that case, the implementation should move
to `--format json` and parse only the assistant text stream.

## Flags not used in v1

OpenCode supports `--pure` which skips external plugins (similar to Claude's
`--bare`). This is not used in v1 because the default behavior is sufficient
for text generation, and `--pure` has not been tested for interactions with
provider loading. If the smoke test reveals plugin-related interference,
`--pure` is the first mitigation to try.

## Branch workflow note

`openspec/changes/turma-plan-opencode/IMPLEMENTATION_DONE.md` is acceptable as a
feature-branch handoff artifact during planning, review, and implementation.
It must still be removed from branch history before the branch is pushed for PR
review unless the team explicitly decides to keep such files as a permanent
public convention.

## Non-goals

- No `--format json` parsing in v1
- No `--attach` server mode for startup cost amortization
- No `--variant` reasoning effort control
- No `--pure` flag (default behavior is sufficient; untested with providers)
- No stdin piping (prompt is passed as positional argument)
- No `--agent` selection (default agent is fine for text generation)
- No `--dir` flag (inherits cwd)
- No Goose backend (separate future work)
- No changes to the critic loop (still not implemented)
