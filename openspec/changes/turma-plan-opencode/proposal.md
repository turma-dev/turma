# Proposal: OpenCode backend for turma plan

## Why

Turma's planning command currently supports Claude and Codex as author
backends. Adding OpenCode extends coverage to a third independent provider
pool — primarily Groq — at significantly lower cost and faster inference
speed. This is a direct step toward the architecture's multi-pool design
where planning work can be routed across providers based on config alone.

## What Changes

- New `OpenCodeAuthorBackend` implementing the existing `AuthorBackend`
  interface
- Updated `_get_backend` routing to select OpenCode for `provider/model`
  format strings (e.g. `groq/llama-3.3-70b-versatile`)
- Updated `turma.example.toml` to document an OpenCode planning example

No changes to the orchestration layer, prompt assembly, artifact validation,
or existing Claude/Codex backends.

## Capabilities

### New Capabilities

- `opencode-planning-backend`: OpenCode author backend for `turma plan`

### Modified Capabilities

None.

## Impact

- `src/turma/authoring/opencode.py` — new file
- `src/turma/planning.py` — `_get_backend` routing extended
- `turma.example.toml` — example config entry added
- `tests/test_authoring_opencode.py` — new file
- `tests/test_planning.py` — backend selection tests extended
- `README.md`, `docs/index.html`, `docs/architecture.md`, `CHANGELOG.md` — follow-up public status update once OpenCode support is implemented and verified
