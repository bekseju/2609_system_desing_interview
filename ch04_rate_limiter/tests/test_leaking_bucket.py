"""1.3 Leaking bucket: FIFO admit/drain, 큐 용량, 고정 처리 간격.
1.9 접수와 실제 처리를 분리해 검증하고, 지속 부하에서 처리 속도와 큐 대기 시간을 측정."""

import statistics

import pytest

from rate_limit_lab.algorithms import LeakingBucket
from rate_limit_lab.models import Status
from tests.algo_helpers import KEY, statuses
from tests.conftest import make_rule


def bucket(**overrides) -> LeakingBucket:
    return LeakingBucket(make_rule(**overrides))  # 기본: Q=10, r=10/s → 간격 100ms


def test_burst_admit_reject_and_processing_times():
    limiter = bucket()
    verdicts = [limiter.admit(KEY, 0, f"r{i:02d}") for i in range(15)]
    assert statuses(verdicts) == ["queued"] * 10 + ["rejected"] * 5
    assert [v.remaining for v in verdicts[:10]] == [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]
    assert all(v.retry_after_ms == 100 and v.reason == "queue_full" for v in verdicts[10:])
    assert limiter.queue_length(KEY) == 10

    # 접수 시점에는 아무것도 처리되지 않았다
    assert limiter.drain(0) == []
    # 250ms까지: 100ms, 200ms 두 건 처리
    events = limiter.drain(250)
    assert [(e.item_id, e.admitted_at_ms, e.processed_at_ms) for e in events] == [("r00", 0, 100), ("r01", 0, 200)]
    assert limiter.queue_length(KEY) == 8
    # 나머지는 300 … 1000ms에 순서대로(FIFO)
    rest = limiter.drain(1000)
    assert [e.item_id for e in rest] == [f"r{i:02d}" for i in range(2, 10)]
    assert [e.processed_at_ms for e in rest] == list(range(300, 1001, 100))
    assert limiter.queue_length(KEY) == 0


def test_slot_frees_exactly_when_head_is_processed():
    limiter = bucket()
    for _ in range(10):
        limiter.admit(KEY, 0)
    assert limiter.admit(KEY, 99).status is Status.REJECTED  # 맨 앞은 100ms에 처리
    v = limiter.admit(KEY, 100, "late")
    assert v.status is Status.QUEUED
    events = limiter.drain(1100)
    late = [e for e in events if e.item_id == "late"][0]
    assert late.processed_at_ms == 1100  # 앞의 10건 뒤에 줄 선다


def test_idle_bucket_processes_after_one_interval():
    limiter = bucket()
    limiter.admit(KEY, 5000, "solo")
    assert limiter.drain(5099) == []
    [event] = limiter.drain(5100)
    assert (event.admitted_at_ms, event.processed_at_ms, event.wait_ms) == (5000, 5100, 100)


def test_decide_is_admit_and_flush_returns_remaining():
    limiter = bucket()
    for t in (0, 10, 20):
        assert limiter.decide(KEY, t).status is Status.QUEUED
    events = limiter.flush()
    assert [e.processed_at_ms for e in events] == [100, 200, 300]
    assert limiter.queue_length(KEY) == 0


def test_keys_have_independent_queues():
    limiter = bucket()
    for key in ("a", "b"):
        for _ in range(10):
            assert limiter.admit(key, 0).status is Status.QUEUED
    assert limiter.admit("a", 0).status is Status.REJECTED
    events = limiter.drain(100)
    assert sorted(e.key for e in events) == ["a", "b"]  # 키마다 창구가 따로 있다


def test_non_integer_interval_is_rounded():
    limiter = bucket(leak_per_sec=3)  # 1000/3 = 333.33… → 333ms (해상도 오차 0.33ms/건)
    assert limiter.drain_interval_ms == 333


@pytest.mark.parametrize("arrival_rate", [5, 10, 20, 50])
def test_sustained_load_rate_and_wait(arrival_rate):
    """1.9: 10초 동안 일정 속도로 보냈을 때 접수·처리 분리, 처리 속도 ≤ 10건/초, 대기 시간 상한."""
    limiter = bucket()
    gap = 1000 // arrival_rate
    times = list(range(0, 10_000, gap))
    verdicts = [limiter.admit(KEY, t, f"r{i:05d}") for i, t in enumerate(times)]
    events = limiter.flush()

    queued = [t for t, v in zip(times, verdicts) if v.status is Status.QUEUED]
    assert len(events) == len(queued)  # 접수한 요청은 모두 정확히 한 번 처리된다
    assert all(e.processed_at_ms > e.admitted_at_ms for e in events)  # 처리는 접수보다 뒤

    processed = [e.processed_at_ms for e in events]
    gaps = [b - a for a, b in zip(processed, processed[1:])]
    assert min(gaps) >= 100  # 처리 간격은 100ms 이상 → 초당 10건 이하
    per_second = [sum(1 for p in processed if s <= p < s + 1000) for s in range(0, 11_000, 1000)]
    assert max(per_second) <= 10

    waits = [e.wait_ms for e in events]
    assert min(waits) >= 100  # 최소 한 간격
    assert max(waits) <= 10 * 100  # 줄 10칸 × 100ms를 넘지 않는다
    if arrival_rate <= 10:
        assert all(v.status is Status.QUEUED for v in verdicts)  # 처리 속도 이하면 거절 없음
        assert max(waits) == 100  # 줄이 쌓이지 않아 한 간격만 기다린다
    else:
        assert statistics.median(waits) == 1000  # 과부하면 줄이 늘 꽉 차 대기 ≈ 1초
