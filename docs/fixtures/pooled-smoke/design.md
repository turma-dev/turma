## Goals

- Give the pooled smoke a deterministic DAG that exercises task-type routing and
  concurrent dispatch without any real engineering work.

## Task DAG

```
        (1) spec  ── root, ready first
         │
   ┌─────┼─────┐
   ▼     ▼     ▼
 (2)impl (3)test (4)docs   ← all [blocked-by: 1]; ready together once (1) merges
```

Each task creates one standalone `*.txt` file (no cross-references), so every
task is trivially completable by any worker backend and the per-task worktrees
never conflict.

## Why converge on a root instead of four independent tasks

`plan-to-beads` makes only the *first* `tasks.md` section independent; any later
section with no `[blocked-by: …]` marker defaults to blocked-by-previous, and an
empty marker is a parse error. So multiple genuinely-independent ready tasks
cannot come from one `tasks.md`. The convergent shape is also the realistic
Turma concurrency model: dependents unblock when their blocker's PR **merges**
(merge-advancement closes the blocker), then run in parallel. The smoke
therefore runs in two phases — root first, then the three dependents at once.

## Routing under the fixture's pooled config

`turma.toml` declares two pools with `max_parallel = 2`:

- `anthropic` (`claude-code`) — serves `impl`, `docs`, `spec`; `default = true`.
- `openai` (`codex`) — serves `test`.

After the root merges, `test` routes to Codex while `impl` / `docs` route to
Claude, so at least one Claude task and the Codex task run concurrently.
