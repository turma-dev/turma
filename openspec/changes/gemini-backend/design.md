## Architecture

The change follows the existing backend-per-CLI pattern in
`src/turma/authoring/`. Each backend is a thin subprocess wrapper that:

1. Validates the CLI binary is on `$PATH` in `__init__`.
2. Builds an argv list and calls `subprocess.run()` in `generate()`.
3. Returns `stdout` on success; raises `PlanningError` on failure or timeout.

### Gemini CLI invocation

```
gemini -p <prompt> -m <model> --approval-mode plan
```

- `-p` / `--prompt` — non-interactive (headless) mode, prints response to
  stdout and exits.
- `-m` / `--model` — model selector (e.g. `gemini-2.5-flash`).
- `--approval-mode plan` — read-only mode. Restricts tool use so the CLI
  cannot shell out, edit files, or mutate the workspace during generation.
  This matches the safety boundary Claude's `--permission-mode plan` provides
  in `claude.py`.

**Trust caveat:** Gemini CLI silently downgrades `--approval-mode plan` to
`default` when the working directory is not trusted
(`config.ts:716`). However, `-p` (headless mode) causes the CLI to treat
the folder as trusted (`trustedFolders.ts:372`), so the downgrade does
not fire for our invocation. This is a fragile implicit dependency — if
Gemini changes its headless-trust behaviour, the read-only guarantee
breaks silently. The backend docstring documents this invariant.

Authentication is handled externally via `GEMINI_API_KEY` env var or prior
OAuth login; the backend does not manage credentials.

### Routing

`_get_backend()` in `planning.py` dispatches on model prefix:

```
gemini-*  →  GeminiAuthorBackend
claude-*  →  ClaudeAuthorBackend
gpt-*/codex-*/o*  →  CodexAuthorBackend
provider/model  →  OpenCodeAuthorBackend
```

The `gemini-*` check is placed before the final `raise` and after the
existing `claude-*` / codex checks.

### Validation guard update

`_validate_feature_relevance` currently rejects any artifact that mentions
`src/turma/authoring/` unless it also mentions `opencode.py`. This must be
widened to allow all known backend filenames (`claude.py`, `codex.py`,
`opencode.py`, `gemini.py`).

`BACKEND_FEATURE_TOKENS` gains `"gemini"` so the relevance guard fires for
gemini-related features.

## File inventory

| File | Action |
|------|--------|
| `src/turma/authoring/gemini.py` | create |
| `src/turma/planning.py` | modify |
| `tests/test_authoring_gemini.py` | create |
| `tests/test_planning.py` | modify |
| `turma.example.toml` | modify |
| `README.md` | modify |
