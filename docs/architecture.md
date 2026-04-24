# Turma Architecture

Turma is a provider-pool-aware multi-agent coding orchestrator.

This document describes the public system model: how planning, task
translation, execution, and recovery are intended to work at a high level. It
is not the internal implementation roadmap.

## Overview

Turma is designed as a two-phase workflow:

1. Planning
   Approved work starts as spec artifacts that are refined through an
   author/critic loop with explicit human review.
2. Execution
   Approved tasks are translated into an executable DAG and routed across
   worker runtimes while task and integration state are tracked explicitly.

The core design idea is simple: provider rate-limit pools are a routing input,
not an implementation detail. Turma uses that fact to spread work across
independent runtimes without pretending that concurrency alone solves planning,
integration, or review.

## System Model

The intended workflow is:

1. Write or refine feature specs.
2. Review and approve the spec with a human gate.
3. Translate approved tasks into a dependency graph.
4. Run task-bounded workers in isolated worktrees or equivalent execution
   environments.
5. Track completion using integration-aware task state rather than "code was
   written" alone.
6. Reconcile task, runtime, and integration state on restart.

## Planning

Planning produces a reviewed feature spec before implementation starts.

The important properties are:

- specs are revised through author/critic iteration
- human approval is explicit, not inferred
- task scoping should produce small, dependency-aware units
- planning output must be concrete enough that implementation workers are not
  forced to guess intent

The committed v1 state model is:

```
drafting -> critic_review -> {needs_revision | awaiting_human_approval}
awaiting_human_approval -> {approved | needs_revision | abandoned}
needs_revision -> drafting (round++)
any state -> needs_human_review
  on: round budget exhaustion
    | critique parse failure
    | repeated unresolved blocking finding IDs across consecutive rounds
needs_human_review -> approved (only via explicit --approve --override)
```

Routing at `critic_review` is decided by the critic's `Status:` token
(`blocking | nits_only | approved`). Blocking rounds always revise;
non-blocking rounds suspend at the human gate. A human must approve
explicitly — critic `approved` never ends the loop on its own.

Revisions use a two-call contract: the author first writes a per-finding
`response_N.md`, then regenerates the spec artifacts using that response
as input. A partial failure between the two calls preserves the response
on disk for retry.

Terminal filesystem markers (`APPROVED`, `ABANDONED.md`,
`NEEDS_HUMAN_REVIEW.md`) are the authoritative indicator of current
state. A LangGraph SQLite checkpoint and a `PLANNING_STATE.json` hint
back up the marker-based authority order but do not override it.

## Task Translation

After approval, Turma turns spec tasks into an executable DAG via the
`turma plan-to-beads` command. The implementation parses the approved
`tasks.md`, gates on the `APPROVED` terminal marker, and creates one
Beads task per numbered section with the section order expressed as
Beads dependency edges.

At a minimum, each task needs:

- a type — parsed as `impl`, `test`, `docs`, or `spec`, translated to
  Beads-native types (`task`, `chore`, `decision`) at transcription
  time; the parser type is also carried through as a `turma-type:<t>`
  label for downstream filtering
- a priority — derived from section order, clamped to Beads' 0-4 scale
- dependency information — default is the previous section, optional
  explicit `[blocked-by: N, M]` markers on the section heading
- acceptance criteria — captured in the Beads task body as the
  verbatim `- [ ]` subtask list

Feature association is recorded via a `feature:<name>` label on every
created task, not via a native Beads epic — `bd` supports epics but
their creation API is still evolving, and label-based grouping is
sufficient for v1 orphan detection and `--force` teardown. Integration
boundaries (`TRANSCRIBED.md` marker file, `bd list --label` orphan
queries) live with the pipeline code.

The task graph is the execution contract. If task boundaries are
wrong, parallel execution becomes unsafe.

## Execution

Execution drives one Beads task at a time from `ready` to `closed`
(or `failed` with a retry-budget decision). The v1 committed
contract lives in `openspec/changes/swarm-orchestration/` and is
implemented by the `turma run` orchestrator.

### State machine

```
preflight → reconcile (read-only) → repair_phase → main_loop
```

`main_loop` runs, per task:

```
fetch_ready → claim → setup_worktree → run_worker → (sentinel dispatch)
  → commit → push → open_pr → close_task        (success path)
  OR
  → fail_task                                    (failure / timeout / clean-tree)
```

### Authority model

When reconciling interrupted state, sources are walked in order of
decreasing authority:

1. **Beads** is the task DAG and the record of which tasks are
   open / claimed / closed / blocked.
2. **Git worktree + branch** records the work in flight for a
   claimed task.
3. **GitHub PR state** records the integration status of a
   submitted task.
4. **Worker sentinel files** (`.task_complete`, `.task_failed`,
   `.task_progress` inside the per-task worktree) are
   worker-to-orchestrator signals only — never authoritative
   current state.

### Retry budget

`bd`'s status vocabulary is `open | in_progress | blocked |
deferred | closed`. Turma does not invent a `failed` state;
retries are persisted through labels:

- `turma-retries:<n>` — attempt counter on the task.
- `needs_human_review` — added on budget exhaustion so ready-task
  listings filter the task out.
- `bd note` — records failure reasons in the task's history.

On each failure the orchestrator reads the current
`turma-retries:<n>` label, calls `BeadsAdapter.fail_task(reason,
retries_so_far, max_retries)`, and the adapter either releases the
claim back to `open` (budget remaining) or adds
`needs_human_review` (exhausted). The orchestrator halts the whole
run on exhaustion so the operator can triage via `bd list --label
needs_human_review`.

### Worker backends

Workers implement a small `WorkerBackend` protocol — `run(invocation)
-> WorkerResult`. v1 registers only `claude-code` (`claude -p
<prompt> --dangerously-skip-permissions` against the per-task
worktree). Workers signal completion by writing sentinel files;
the orchestrator never parses worker stdout for success / failure.

Codex / OpenCode / Gemini worker backends are v2 concerns. The
protocol is deliberately minimal so adding a backend is a small
branch.

### Reconciliation

Reconciliation runs before every invocation (including
`--dry-run`) and is **strictly read-only**. It walks the authority
model and returns a typed `ReconciliationReport` classifying each
in-progress task into one of six finding types
(`missing-worktree`, `completion-pending`,
`completion-pending-with-pr`, `failure-pending`,
`stale-no-sentinels`, `orphan-branch`). The orchestrator's repair
phase — the only mutation-carrying phase before the main loop —
consumes the report in order and applies the documented repair
per finding type, halting before `fetch_ready` if any finding is
`stale-no-sentinels` (v1 never guesses on ambiguous state) or if
any repair-phase failure exhausts the retry budget.

### Multi-runtime roadmap

Turma is designed to support multiple worker runtimes via
`[swarm] worker_backend` plus per-task `worker-backend:<id>`
labels for routing. v1 ships the sequential single-backend loop;
parallel execution and per-task routing are explicitly deferred.

## Task State

Task state needs to distinguish authored work from integrated work.

A practical lifecycle looks like:

`ready -> claimed -> in_progress -> code_complete -> pr_open -> ci_green -> merged -> done`

with side states such as:

`failed`
`human_review`

The exact storage layer may change, but the semantics matter more than the
names: downstream tasks should not treat "PR opened" as equivalent to
completion.

## Recovery And Reconciliation

Turma is intended to recover by reconciling external state, not by assuming an
in-memory session can be resumed perfectly.

Authoritative state should be checked in this order:

1. task registry state
2. integration state such as PR or merge status
3. local or provider runtime metadata

If those sources disagree, Turma should prefer the more authoritative external
record and surface ambiguous cases for review rather than guessing.

## Current Public Status

Today, the public repo contains:

- a Python package scaffold
- OpenSpec workflow scaffolding
- a working `turma init` command
- a working `turma plan` command running the full author/critic loop with
  max-rounds and loop-detection guards, a resume CLI
  (`--approve | --revise | --abandon | --approve --override`), and
  LangGraph SQLite checkpointing, backed by Claude, Codex, Gemini, and
  OpenCode authoring backends
- a working `turma plan-to-beads` command that transcribes approved
  plans into feature-tagged Beads task sets
- a working `turma run` single-feature sequential swarm orchestrator
  (preflight → reconcile → repair → main loop, one PR per Beads
  task) with the `claude-code` worker backend
- minimal CI for install and test validation
- project configuration and validation basics
- architecture and workflow documentation

Planning quality still depends on the chosen provider/model. Parallel
execution, per-task backend routing, and Codex / OpenCode / Gemini
worker backends are deferred past v1.

## Scope Of This Document

This document intentionally stays high level.
