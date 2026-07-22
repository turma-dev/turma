"""Emitter identity + thread-safety (run-v1-stream-identity).

`JsonEmitter` stamps every event with a stable per-run `run_id` and an ISO-8601
`ts`, and both emitters serialize their writes so concurrent worker-thread events
(the concurrent dispatcher emits from several threads, in text mode too) stay
line-atomic instead of interleaving into corrupt output.
"""

from __future__ import annotations

import io
import json
import threading
import time
from datetime import datetime

from turma.swarm.events import (
    RUN_SCHEMA,
    HeartbeatTicker,
    JsonEmitter,
    TextEmitter,
)


def test_json_emitter_injects_stable_run_id_and_iso_ts() -> None:
    buf = io.StringIO()
    em = JsonEmitter(stream=buf)
    em.emit("task_claimed", task_id="t1")
    em.emit("commit", task_id="t1")

    objs = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert len(objs) == 2
    for obj in objs:
        assert obj["schema"] == RUN_SCHEMA
        assert obj["run_id"]  # non-empty
        datetime.fromisoformat(obj["ts"])  # raises if not ISO-8601
    # One run_id for the whole run, exposed on the emitter.
    assert objs[0]["run_id"] == objs[1]["run_id"] == em.run_id


def test_json_emitter_run_id_is_pinnable() -> None:
    buf = io.StringIO()
    em = JsonEmitter(stream=buf, run_id="fixed123")
    em.emit("commit", task_id="t1")
    assert json.loads(buf.getvalue())["run_id"] == "fixed123"


class _InterleavingStream:
    """Writes each string in two halves with a yield between — a non-atomic
    (unlocked) caller interleaves; a caller that serializes whole writes does
    not. Exposes whether the emitter locks around its write."""

    def __init__(self) -> None:
        self._out = io.StringIO()

    def write(self, s: str) -> int:
        half = len(s) // 2
        self._out.write(s[:half])
        time.sleep(0.0005)  # widen the interleave window
        self._out.write(s[half:])
        return len(s)

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        return self._out.getvalue()


def _hammer(emitter, n_threads: int, n_each: int) -> None:
    barrier = threading.Barrier(n_threads)

    def worker(k: int) -> None:
        barrier.wait()
        for i in range(n_each):
            emitter.emit("commit", task_id=f"t{k}-{i}")

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_json_emitter_concurrent_emits_stay_line_atomic() -> None:
    stream = _InterleavingStream()
    em = JsonEmitter(stream=stream)
    _hammer(em, n_threads=8, n_each=10)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 80
    for line in lines:
        json.loads(line)  # each line is intact JSON — raises if interleaved


def test_text_emitter_concurrent_prints_stay_intact() -> None:
    stream = _InterleavingStream()
    em = TextEmitter(stream=stream)
    _hammer(em, n_threads=8, n_each=10)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 80
    assert all(line.startswith("commit: ") for line in lines)


def test_heartbeat_ticker_fires_periodically_then_stops() -> None:
    buf = io.StringIO()
    em = JsonEmitter(stream=buf)
    ticker = HeartbeatTicker(
        em, interval_seconds=0.02, started_at=time.monotonic()
    ).start()
    time.sleep(0.11)  # ~5 intervals
    ticker.stop()

    heartbeats = [
        json.loads(line)
        for line in buf.getvalue().splitlines()
        if json.loads(line)["event"] == "heartbeat"
    ]
    assert len(heartbeats) >= 1
    assert all("elapsed_ms" in h and h["schema"] == RUN_SCHEMA for h in heartbeats)

    # After stop, no further heartbeats fire.
    n_after_stop = len(buf.getvalue().splitlines())
    time.sleep(0.06)
    assert len(buf.getvalue().splitlines()) == n_after_stop


def test_heartbeat_ticker_disabled_when_interval_zero() -> None:
    buf = io.StringIO()
    em = JsonEmitter(stream=buf)
    ticker = HeartbeatTicker(em, interval_seconds=0, started_at=time.monotonic())
    ticker.start()  # no thread spawned
    time.sleep(0.05)
    ticker.stop()
    assert buf.getvalue() == ""
