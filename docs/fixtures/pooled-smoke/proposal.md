## Why

A disposable fixture for the **pooled / parallel** `turma run` smoke
(`docs/smoke-pooled-parallel.md`). It is not a real product change — its only
purpose is to give the concurrent multi-pool dispatcher a small, deterministic
task DAG to route across two backends.

## What changes

Four trivial file-creation tasks with mixed `turma-type:` labels (`spec`,
`impl`, `test`, `docs`) arranged as a convergent DAG: one `spec` root, then
`impl` / `test` / `docs` each blocked by the root. Once the root's PR merges,
the three dependents become ready together and dispatch concurrently — `test`
to the Codex pool, `impl` / `docs` to the Anthropic pool — which is what the
smoke observes.
