"""2.7 같은 CSV를 모든 알고리즘에 주입하는 결정적 재생기."""

from collections import Counter
from dataclasses import replace
from functools import cache

import pytest

from rate_limit_lab.algorithms import ALGORITHMS
from rate_limit_lab.clock import replay_order
from rate_limit_lab.config import load_rules
from rate_limit_lab.load.replay import replay
from rate_limit_lab.load.scenarios import generate, load_scenarios, sha256_of, write_generated
from rate_limit_lab.models import Status, read_decisions
from tests.conftest import run_python

RULES = load_rules()
NAMES = sorted(ALGORITHMS)


@cache
def overload_short():
    """overload를 10초로 줄인 버전 (2~4초에 10배 추가)."""
    base = load_scenarios()["overload"]
    comps = (
        {**base.components[0], "end_ms": 10_000},
        {**base.components[1], "start_ms": 2_000, "end_ms": 4_000},
    )
    return generate(replace(base, duration_ms=10_000, components=comps), 42).requests


@pytest.mark.parametrize("name", NAMES)
def test_every_request_gets_one_decision_in_replay_order(name):
    requests = overload_short()
    result = replay(requests, RULES, name)
    assert [d.request_id for d in result.decisions] == [r.request_id for r in replay_order(requests)]
    counts = result.stats["status_counts"]
    assert sum(counts.values()) == len(requests) == result.stats["input_requests"]
    assert result.stats["accepted"] + result.stats["rejected"] == len(requests)


@pytest.mark.parametrize("name", NAMES)
def test_same_input_same_decisions(name):
    requests = overload_short()
    a = replay(requests, RULES, name, measure_latency=False)
    b = replay(list(reversed(requests)), RULES, name, measure_latency=False)  # 입력 순서가 달라도
    assert a.decisions == b.decisions
    assert a.events == b.events
    assert a.stats == b.stats
    # 지연을 재는 경우에도 지연 외 모든 값은 같다
    c = replay(requests, RULES, name)
    assert [replace(d, latency_us=0.0) for d in c.decisions] == a.decisions
    assert all(d.latency_us > 0 for d in c.decisions)


@pytest.mark.parametrize("name", NAMES)
def test_events_are_chronological_and_complete(name):
    requests = overload_short()
    result = replay(requests, RULES, name)
    times = [e.at_ms for e in result.events]
    assert times == sorted(times)
    kinds = Counter(e.event for e in result.events)
    assert kinds["arrival"] == kinds["decision"] == len(requests)
    first = {}
    for e in result.events:
        first.setdefault((e.request_id, e.event), e.at_ms)
    for d in result.decisions:
        assert first[(d.request_id, "arrival")] == d.arrival_at_ms == d.decision_at_ms
        if d.processed_at_ms is not None:
            assert first[(d.request_id, "processed")] == d.processed_at_ms > d.decision_at_ms


def test_leaking_bucket_admission_and_processing_are_separate():
    result = replay(overload_short(), RULES, "leaking_bucket")
    decision_status = Counter(e.status for e in result.events if e.event == "decision")
    processed = [e for e in result.events if e.event == "processed"]
    assert set(decision_status) == {"queued", "rejected"}
    assert len(processed) == decision_status["queued"]  # 접수한 요청은 모두 처리된다
    assert result.stats["status_counts"]["processed"] == decision_status["queued"]
    assert result.stats["status_counts"]["queued"] == 0  # 끝까지 처리했으므로 최종 상태에 queued는 없다
    assert result.stats["max_processed_per_key_in_moving_window"] <= 10


@pytest.mark.parametrize("name", NAMES)
def test_eviction_during_replay_does_not_change_decisions(name):
    requests = overload_short()
    a = replay(requests, RULES, name, measure_latency=False, evict_interval_ms=None)
    b = replay(requests, RULES, name, measure_latency=False, evict_interval_ms=250)
    assert a.decisions == b.decisions


def test_cli_outputs_identical_across_runs(tmp_path):
    write_generated(tmp_path / "in", generate(load_scenarios()["boundary"], 42))
    hashes = []
    for run in ("a", "b"):
        out = tmp_path / run
        result = run_python("-m", "rate_limit_lab.load.run_replay", str(tmp_path / "in" / "requests.csv"),
                            "--out", str(out), "--no-latency")
        assert result.returncode == 0, result.stderr
        files = sorted(p for p in out.rglob("*") if p.is_file())
        hashes.append({p.relative_to(out).as_posix(): sha256_of(p) for p in files})
    assert hashes[0] == hashes[1]
    assert {f"{n}/decisions.csv" for n in NAMES} <= set(hashes[0])
    assert {"accuracy.json", "accuracy.csv"} <= set(hashes[0])
    decisions = read_decisions(tmp_path / "a" / "fixed_window" / "decisions.csv")
    assert [d.status for d in decisions] == [Status.ALLOWED] * 20
