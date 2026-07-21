# Pooled-smoke fixture

A disposable, versioned fixture for the pooled / parallel `turma run` smoke.
Copy it into a scratch repo (see `docs/smoke-pooled-parallel.md`); it is not a
real product change.

## Contents

| file | copies to (scratch repo) | purpose |
|---|---|---|
| `turma.example.toml` | repo root as `turma.toml` | pooled config: `max_parallel = 2`, two pools (`claude-code` for `impl`/`docs`/`spec`, `codex` for `test`). Named `.example.` because a bare `turma.toml` is gitignored. |
| `proposal.md` | `openspec/changes/pooled-smoke/proposal.md` | change stub |
| `design.md` | `openspec/changes/pooled-smoke/design.md` | the task DAG + routing rationale |
| `tasks.md` | `openspec/changes/pooled-smoke/tasks.md` | 4 tasks, mixed `turma-type:` labels, convergent DAG |

## Task DAG

`spec` root → `impl` / `test` / `docs` each `[blocked-by: 1]`. The three
dependents become ready together once the root's PR merges, then dispatch
concurrently and route by type: `test` → Codex, `impl` / `docs` → Claude.

`tests/test_pooled_smoke_fixture.py` pins this shape (parsed types + DAG +
routing) so the fixture can't silently drift away from what the runbook
describes.
