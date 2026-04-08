## Why

Turma supports Claude, OpenCode, and Codex as planning author backends but not
Google Gemini. Gemini CLI (`gemini`) provides free-tier access (60 req/min,
1 000/day) and a non-interactive mode (`-p`) that fits the existing backend
pattern. Adding it widens provider coverage and lets users plan with Gemini
models without going through OpenCode's provider/model routing.

## What Changes

- New `GeminiAuthorBackend` in `src/turma/authoring/gemini.py` wrapping the
  `gemini` CLI.
- Updated model routing in `src/turma/planning.py` to dispatch `gemini-*`
  prefixed models to the new backend.
- Relaxed `_validate_feature_relevance` guard so generated artifacts that
  reference `src/turma/authoring/gemini.py` are not incorrectly rejected.
- New unit tests in `tests/test_authoring_gemini.py`.
- New routing and validation tests in `tests/test_planning.py`.
- Updated config example in `turma.example.toml`.
- Updated supported-backends list in `README.md`.

## Capabilities

### New Capabilities

- `gemini-authoring`: Gemini CLI author backend for the planning phase.

### Modified Capabilities

- `author-routing`: `_get_backend()` gains a `gemini-*` branch and the
  validation guard allowlists the new backend path.

## Impact

- New file: `src/turma/authoring/gemini.py`
- New file: `tests/test_authoring_gemini.py`
- Modified: `src/turma/planning.py` (import, routing, validation)
- Modified: `tests/test_planning.py` (backend selection + validation coverage)
- Modified: `turma.example.toml` (commented example)
- Modified: `README.md` (supported backends list)
- Runtime dependency: `gemini` CLI must be installed (`npm i -g @google/gemini-cli`)
- No breaking changes to existing backends.
