"""1.8 모든 알고리즘의 복수 키 격리, 용량 경계, TTL·유휴 상태 제거."""

import random

import pytest

from rate_limit_lab.algorithms import ALGORITHMS, create
from tests.algo_helpers import accepted_count
from tests.conftest import make_rule

NAMES = sorted(ALGORITHMS)


@pytest.mark.parametrize("name", NAMES)
def test_capacity_boundary(name):
    # 기본 정책은 모든 알고리즘에서 '동시에 10건'까지 받아들인다
    limiter = create(name, make_rule())
    assert accepted_count(limiter.decide("k", 0) for _ in range(10)) == 10
    assert accepted_count(limiter.decide("k", 0) for _ in range(5)) == 0


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("limit", [1, 3])
def test_capacity_boundary_small_limits(name, limit):
    limiter = create(name, make_rule(limit=limit, bucket_capacity=limit, queue_capacity=limit))
    assert accepted_count(limiter.decide("k", 0) for _ in range(limit + 5)) == limit


@pytest.mark.parametrize("name", NAMES)
def test_multi_key_isolation(name):
    limiter = create(name, make_rule())
    # alice가 한도를 다 써도 bob은 영향 없음
    assert accepted_count(limiter.decide("alice", 0) for _ in range(50)) == 10
    assert accepted_count(limiter.decide("bob", 0) for _ in range(10)) == 10
    # 100명이 동시에 10건씩 → 모두 10건씩
    for i in range(100):
        assert accepted_count(limiter.decide(f"user-{i}", 1) for _ in range(10)) == 10


@pytest.mark.parametrize("name", NAMES)
def test_idle_keys_are_evicted_after_ttl(name):
    limiter = create(name, make_rule())  # idle_ttl_ms = 2000
    for i in range(100):
        for _ in range(10):
            limiter.decide(f"user-{i}", 0)
    before_keys, before_entries = limiter.active_keys(), limiter.state_entries()
    assert before_keys == 100

    assert limiter.evict_idle(1999) == 0  # TTL(2000ms) 전에는 지우지 않는다
    assert limiter.active_keys() == 100

    limiter.decide("user-0", 1999)  # user-0은 방금 다시 사용 → 남아야 한다
    assert limiter.evict_idle(2000) == 99
    assert limiter.active_keys() == 1
    assert limiter.state_entries() < before_entries


@pytest.mark.parametrize("name", NAMES)
def test_busy_keys_are_not_evicted(name):
    # 한도를 초과해 상태가 '처음과 다른' 키는 TTL이 짧아도 지우지 않는다
    limiter = create(name, make_rule(idle_ttl_ms=1))
    for _ in range(10):
        limiter.decide("k", 0)
    assert limiter.evict_idle(1) == 0
    assert limiter.active_keys() == 1


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("seed", range(3))
def test_eviction_never_changes_decisions(name, seed):
    """매 요청마다 제거를 돌린 결과와 전혀 제거하지 않은 결과가 같아야 한다."""
    rng = random.Random(seed)
    t, trace = 0, []
    for _ in range(3000):
        t += rng.choice([0, 0, 1, 5, 20, 100, 700, 1500, 3000])
        trace.append((f"user-{rng.randrange(20)}", t))

    rule = make_rule(idle_ttl_ms=rng.choice([1, 500, 2000]))
    with_eviction, without = create(name, rule), create(name, rule)
    got, expected = [], []
    for key, now in trace:
        with_eviction.evict_idle(now)
        got.append(with_eviction.decide(key, now))
        expected.append(without.decide(key, now))
    assert got == expected
    assert with_eviction.active_keys() <= without.active_keys()
