## Tasks

### 1. Create `src/turma/authoring/gemini.py`

- [ ] Implement `GeminiAuthorBackend(AuthorBackend)`.
- [ ] `__init__`: validate `shutil.which("gemini")`.
- [ ] `generate`: invoke
      `["gemini", "-p", prompt, "-m", model, "--approval-mode", "plan"]`
      via `subprocess.run`, capture stdout, handle timeout and non-zero exit
      using `extract_process_error`.

### 2. Update `src/turma/planning.py`

- [ ] Import `GeminiAuthorBackend`.
- [ ] Add `model.startswith("gemini-")` branch in `_get_backend()`.
- [ ] Update error message to include `gemini-*`.
- [ ] Add `"gemini"` to `BACKEND_FEATURE_TOKENS`.
- [ ] Widen `_validate_feature_relevance` authoring-path check to allow all
      known backend files, not just `opencode.py`.

### 3. Create `tests/test_authoring_gemini.py`

- [ ] `test_generate_returns_stdout_on_success`
- [ ] `test_generate_raises_on_non_zero_exit`
- [ ] `test_generate_uses_stdout_when_stderr_empty`
- [ ] `test_generate_raises_on_timeout`
- [ ] `test_backend_init_requires_gemini_cli`
- [ ] `test_generate_uses_correct_command_structure`

### 4. Update `tests/test_planning.py`

- [ ] Add `test_get_backend_selects_gemini_for_gemini_models` — assert
      `_get_backend("gemini-2.5-flash")` returns `GeminiAuthorBackend`.
- [ ] Update `test_get_backend_rejects_unknown_model_prefix` if needed to
      ensure `gemini-*` is no longer rejected.
- [ ] Add a test that `_validate_artifact_output` accepts artifact text
      mentioning `src/turma/authoring/gemini.py` for a gemini-related
      feature (mirrors the existing opencode allowlist test).

### 5. Update `turma.example.toml`

- [ ] Add commented `author_model = "gemini-2.5-flash"` line under
      `[planning]`.

### 6. Update `README.md`

- [ ] Add Gemini to the supported backends list (currently: Claude, Codex,
      OpenCode) around line 68.
- [ ] Note `gemini` CLI install requirement
      (`npm install -g @google/gemini-cli`).

### 7. Verify

- [ ] `uv run pytest tests/test_authoring_gemini.py -v` passes.
- [ ] `uv run pytest tests/test_planning.py -v` passes.
- [ ] `uv run pytest tests/ -v` — no regressions.
