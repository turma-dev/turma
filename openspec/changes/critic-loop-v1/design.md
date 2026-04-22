## Scope

Covers the author/critic iteration between `turma plan` kickoff and
the handoff to any downstream task-translation step. Ends with a
human-written `APPROVED` signal, an abandonment marker, or a halted
review state.

Artifacts covered: `proposal.md`, `design.md`, `tasks.md`. The
`specs/` artifact is intentionally deferred — it will be added as a
separate feature once OpenSpec `specs/` generation is implemented.

Out of scope: task translation, worktree orchestration, any work
after `APPROVED` is written.

## Authority model

> Critic recommends. Human approves. Graph records. Git preserves.

- Only a human creates the final `APPROVED` signal.
- Critic status `approved` fast-tracks to the human gate; it never
  ends the loop on its own.
- Human override of a blocking critique is available **only** from
  the halted `needs_human_review` state. During the active loop,
  blocking critiques always route to revision. This keeps the state
  machine simple; a per-round override gate is a v2 concern.

Deliberate v1 tradeoff: if the critic is wrong in an early round,
the user either lets the author revise (and checks whether the next
round still blocks) or waits for terminal escalation and overrides
from `needs_human_review`.

## State machine

```
drafting ──▶ critic_review ──▶ {needs_revision | awaiting_human_approval}

awaiting_human_approval ──▶ {approved (END) | needs_revision | abandoned (END)}

needs_revision ──▶ drafting (round++)

any state ──▶ needs_human_review
    on: round budget exhaustion
      | critique parse failure
      | repeated identical blocking finding ID set

needs_human_review ──▶ approved (END)
    only via: --approve --override "<reason>"
```

`needs_human_review` is a halted review state: terminal for
automatic planning, but manually overridable into `approved`. A
single `--override` flag covers every cause that can land in
`needs_human_review` — it is not specific to blocking critiques.

### critic_review routing

| Critic status | Next state                               |
|---------------|------------------------------------------|
| `blocking`    | `needs_revision` (no human gate in v1)   |
| `nits_only`   | `awaiting_human_approval`                |
| `approved`    | `awaiting_human_approval` (fast-track)   |

Routing is decided by the `Status` line alone. Per-finding labels
(`[blocking]`, `[nits]`, `[question]`) are detail, not route inputs.
Questions under `Status: blocking` are blocking until answered;
questions under `Status: nits_only` are advisory.

### Round numbering

Initial author generation is round 1. Its critic output is
`critique_1.md`. If round 1 routes to `needs_revision`,
`response_1.md` is written; the revised draft becomes round 2.
There is no round 0.

### `interactive` config behavior

- `interactive = true` (default): human resume gates are active.
  `awaiting_human_approval` suspends the graph until a resume
  command is issued.
- `interactive = false`: the graph halts at `awaiting_human_approval`,
  prints the exact `turma plan --resume …` commands the human would
  use, and exits. It does **not** auto-approve. Approval must be an
  explicit follow-up. The persisted state (LangGraph checkpoint plus
  filesystem artifacts) remains resumable by the same commands as
  the interactive path. A non-interactive halt is not a failed run.

## Filesystem contract

All paths relative to `openspec/changes/<feature>/`.

| File                    | Writer  | Purpose |
|-------------------------|---------|---------|
| `proposal.md`           | author  | Feature proposal artifact. |
| `design.md`             | author  | Technical design artifact. |
| `tasks.md`              | author  | Atomic task list for downstream translation. |
| `critique_N.md`         | critic  | Round-N critique in strict format (see below). |
| `response_N.md`         | author  | Per-finding accept/reject responding to `critique_N.md`. The revised draft is round N+1. |
| `response_N_human.md`   | human   | Human revision reasons. `N` matches the round of the latest `critique_N.md` / approval gate the human is responding to. |
| `PLANNING_STATE.json`   | graph   | Recovery hint; never authoritative. |
| `APPROVED`              | human   | Sole final-approval signal. |
| `ABANDONED.md`          | human   | Terminal abandonment marker: reason, timestamp, round, actor. |
| `NEEDS_HUMAN_REVIEW.md` | graph   | Terminal failure reason (budget exhausted, parse failure, loop detection). |
| `OVERRIDE.md`           | human   | Written when the human overrides `needs_human_review`; contains reason. Not a final-approval signal on its own. |

## Critique format

Critic output is parsed, not free-read. The header is
machine-readable; finding bodies may be prose.

```
## Status: blocking

## Findings
- [B001] [blocking] [design.md] Retry budget undefined for spec task type
- [N003] [nits]     [tasks.md]  Task "wire auth" could be two tasks
- [Q002] [question] [proposal.md] Does this cover OAuth refresh?
```

### Status tokens

Exactly one of `blocking`, `nits_only`, `approved`. Any other value,
or a missing `## Status:` line, hard-routes the round to
`needs_human_review`.

### Finding IDs

- Format: `<prefix><zero-padded counter>`. `B` = blocking,
  `N` = nits, `Q` = question.
- Counter is stable across rounds: if an issue recurs, the critic
  reuses its ID; genuinely new issues get new IDs.
- File path MUST be included. Line numbers MAY appear but are NOT
  used for identity comparison.
- Missing or malformed finding IDs hard-route the round to
  `needs_human_review`.

### Loop-detection compliance note

Loop detection compares unresolved blocking finding ID sets across
rounds. This depends on the critic reusing stable IDs for recurring
issues and minting new IDs for new ones. A non-compliant critic may
cause premature escalation or over-long looping. v1 accepts this as
best-effort; a future iteration may normalize file path plus message
text as a secondary progress signal.

## Author revision contract

On `needs_revision`, the author MUST produce `response_N.md`
(per-finding accept/reject, keyed by finding ID, with rationale) as
a discrete artifact before any revised spec file is touched.

Enforced by invoking the author backend in two distinct calls:

1. Generate `response_N.md` given `critique_N.md` plus prior
   artifacts.
2. Generate revised `proposal.md` / `design.md` / `tasks.md` given
   `response_N.md`.

Convention-only prompting ("please write response.md first") is
insufficient — the two-call contract is the implementation
requirement.

## Convergence signals

- Critic status `approved` + human writes `APPROVED` → END
  (`approved`).
- Critic status `nits_only` → `awaiting_human_approval`. Human
  chooses approve / revise / abandon.
- Critic status `blocking` → `needs_revision`. No human option in
  v1.
- Halted `needs_human_review` may be overridden to `approved` by
  the human via `--approve --override`.

### Approval with outstanding nits

Approving from `nits_only` does NOT require the author to clear the
nits. Outstanding nits are intentionally accepted as non-blocking
and remain in the round's `critique_N.md` as audit history only.

## Round budget

- `max_rounds` default = 4 (already in `turma.example.toml`).
- Loop detection: compare the SET of unresolved blocking finding IDs
  at the end of each round. Two consecutive rounds with identical
  unresolved blocking ID sets → `needs_human_review`. Message or
  line-number changes do not count as progress.
- Round-budget exhaustion never auto-approves. It always routes to
  `needs_human_review`.

## Human resume contract

The planning graph suspends at gated states via LangGraph
`interrupt_before`. Resume commands inject a structured payload
into the approval node; filesystem markers are produced as a side
effect of the command, not as the trigger for the transition.

```
turma plan --resume <feature>
```

Read-only status: loads/reconstructs the suspended state and
reports it. MUST NOT mutate state or advance the graph.

```
turma plan --resume <feature> --approve
```

Writes `APPROVED`. Allowed from `awaiting_human_approval` only (the
override variant below is the exception).

```
turma plan --resume <feature> --revise "<why>"
```

Writes `response_N_human.md` with the reason. Allowed from
`awaiting_human_approval`. Advances to `needs_revision` and
increments the round counter.

```
turma plan --resume <feature> --abandon "<why>"
```

Writes `ABANDONED.md` (reason, timestamp, round, actor). Abandon
reason lives in `ABANDONED.md` only; `--abandon` does NOT produce
`response_N_human.md`. Terminal END, no `APPROVED` produced.

```
turma plan --resume <feature> --approve --override "<why>"
```

Writes `OVERRIDE.md` then `APPROVED`. Allowed ONLY from halted
`needs_human_review`. Covers every halt cause in v1 (repeated
blocking findings, critique parse failure, round budget
exhaustion).

## Recovery and reconciliation

### `PLANNING_STATE.json`

```json
{
  "feature": "...",
  "round": 2,
  "state": "awaiting_human_approval",
  "last_commit": "<sha>",
  "last_critique": "critique_2.md",
  "critic_status": "nits_only",
  "updated_at": "..."
}
```

Recovery hint only. Never a source of truth.

### Authoritative sources on restart

Highest authority first for current workflow state:

1. Terminal artifacts: `APPROVED`, `NEEDS_HUMAN_REVIEW.md`,
   `ABANDONED.md`.
2. Latest valid `critique_N.md` / `response_N.md` /
   `response_N_human.md`.
3. LangGraph SQLite checkpoint.
4. Git commit history.
5. `PLANNING_STATE.json`.

Git commits are the audit trail, not the current-state decider.
Filesystem terminal markers win. On disagreement, prefer the
higher layer and log the reconciliation.

### Terminal approval authority

- `APPROVED` is the sole final-approval signal.
- `OVERRIDE.md` by itself is NOT approval; it is evidence that an
  override was initiated.
- If `OVERRIDE.md` exists without `APPROVED` (e.g. crash between
  writes), the loop is NOT approved. Recovery treats it as
  `needs_human_review` pending human re-confirmation.
- The override command writes `OVERRIDE.md` first and `APPROVED`
  second, so partial state always fails safe.

## Git commit granularity

Per-phase commits within a round, not one commit per round.

| Message                                | When written |
|----------------------------------------|--------------|
| `spec(<feature>): round N draft`       | After author draft/revision. |
| `spec(<feature>): round N critique`    | After `critic_review`. |
| `spec(<feature>): approved`            | Once at END, when `APPROVED` is written (plus `OVERRIDE.md` if applicable). |
| `spec(<feature>): abandoned`           | Once at END, when `ABANDONED.md` is written. |
| `spec(<feature>): needs human review`  | Once at END, for the terminal failure path. |

`response_N.md` is committed with the draft commit of round N+1
(it is an input to that draft). File-level commits beyond this
granularity are rejected.

### Partial-failure rule

If `response_N.md` is successfully generated but revised-draft
generation then fails, `response_N.md` remains UNCOMMITTED on the
filesystem. Retry resumes from the filesystem artifact and commits
once both `response_N.md` and the revised draft exist. No partial
"response-only" commit.

## Critique scope (v1)

Full-read: the critic reads the whole artifact set plus prior
unresolved findings each round. Diff-scoped critique is a later
optimization — cheaper, but risks missing global inconsistency.

## Dependencies and storage

- New Python dependency: `langgraph` plus a SQLite checkpointer
  (e.g. `langgraph-checkpoint-sqlite`). Added to `pyproject.toml`.
- Checkpoint files live at `./.langgraph/<feature>.db` and are
  gitignored. Deleting the file does not lose durable state — the
  loop reconstructs from filesystem artifacts per the recovery
  authority order.
- `PLANNING_STATE.json` is written alongside artifacts in the
  change directory as generated planning runtime metadata. It is not
  committed by default and should be ignored unless a user explicitly
  chooses to preserve a planning transcript for review.

## Open items deferred past v1

- Diff-scoped critique.
- Splitting the critic into requirements-critic + task-structure-critic
  passes.
- `specs/` artifact once OpenSpec `specs/` generation lands.
- Per-round human override of blocking critiques.
