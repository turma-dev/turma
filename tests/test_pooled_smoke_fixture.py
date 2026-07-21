"""Guard the pooled-smoke fixture (docs/fixtures/pooled-smoke/) against drift.

The fixture backs the pooled/parallel smoke (docs/smoke-pooled-parallel.md).
Its whole value is that `plan-to-beads` transcribes `tasks.md` into a specific
DAG (a `spec` root; `impl`/`test`/`docs` each blocked by it) and that the pooled
`turma.toml` routes those types across two backends. These are pure parses — no
`bd`/`git` — so we can pin the contract the runbook depends on.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from turma.config import _parse_swarm, build_swarm_router
from turma.transcription.tasks_md import ParsedTasks, parse_tasks_md

_FIXTURE = Path(__file__).resolve().parent.parent / "docs" / "fixtures" / "pooled-smoke"


def test_tasks_md_transcribes_to_the_convergent_dag() -> None:
    result = parse_tasks_md((_FIXTURE / "tasks.md").read_text())
    assert isinstance(result, ParsedTasks), result
    sections = result.sections
    assert [s.task_type.value for s in sections] == ["spec", "impl", "test", "docs"]
    # spec is the independent root; impl/test/docs each converge on it, so they
    # become ready together once the root merges — the concurrency the smoke shows.
    assert [s.blocked_by for s in sections] == [(), (1,), (1,), (1,)]


def test_turma_toml_routes_types_across_two_backends() -> None:
    raw = tomllib.loads((_FIXTURE / "turma.example.toml").read_text())
    swarm = _parse_swarm(raw["swarm"])
    assert swarm.max_parallel == 2
    assert {p.name for p in swarm.pools} == {"anthropic", "openai"}

    router = build_swarm_router(swarm)
    # test → Codex; every other type (incl. the default fall-through) → Claude.
    assert router.pool_for("test").backend == "codex"
    for t in ("impl", "docs", "spec", "chore"):
        assert router.pool_for(t).backend == "claude-code"


def test_backend_override_collapses_fixture_pools() -> None:
    raw = tomllib.loads((_FIXTURE / "turma.example.toml").read_text())
    swarm = _parse_swarm(raw["swarm"])
    router = build_swarm_router(swarm, backend_override="opencode")
    assert len(router.pools) == 1
    assert router.pools[0].backend == "opencode"
