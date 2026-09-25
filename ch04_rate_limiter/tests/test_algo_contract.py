"""1.1 공통 decide(key, time) 인터페이스와 상태·유휴 키 제거 계약."""

import pytest

from rate_limit_lab.algorithms import ALGORITHMS, RateLimiter, Verdict, create
from rate_limit_lab.clock import ClockError
from rate_limit_lab.models import Status
from tests.conftest import make_rule

NAMES = sorted(ALGORITHMS)


def test_registry_has_all_algorithms():
    assert NAMES == sorted(
        ["token_bucket", "leaking_bucket", "fixed_window", "sliding_log", "sliding_log_pdf", "sliding_counter"]
    )
    with pytest.raises(ValueError, match="알 수 없는 알고리즘"):
        create("magic", make_rule())


@pytest.mark.parametrize("name", NAMES)
def test_decide_returns_verdict_fields(name):
    limiter = create(name, make_rule())
    assert isinstance(limiter, RateLimiter)
    assert limiter.name == name
    verdict = limiter.decide("k", 0)
    assert isinstance(verdict, Verdict)
    assert verdict.status in (Status.ALLOWED, Status.QUEUED)
    assert verdict.accepted
    assert verdict.remaining == 9
    assert verdict.retry_after_ms is None
    assert verdict.reason


@pytest.mark.parametrize("name", NAMES)
def test_rejection_has_retry_after_and_reason(name):
    limiter = create(name, make_rule())
    verdicts = [limiter.decide("k", 0) for _ in range(11)]
    last = verdicts[-1]
    assert last.status is Status.REJECTED
    assert last.remaining == 0
    assert last.retry_after_ms is not None and last.retry_after_ms > 0
    assert last.reason


@pytest.mark.parametrize("name", NAMES)
def test_time_must_not_go_backward(name):
    limiter = create(name, make_rule())
    limiter.decide("a", 100)
    limiter.decide("b", 100)  # 같은 시각은 허용
    with pytest.raises(ClockError, match="시계 역행"):
        limiter.decide("a", 99)
    with pytest.raises(ClockError, match="정수 ms"):
        limiter.decide("a", 100.5)


@pytest.mark.parametrize("name", NAMES)
def test_state_is_per_key_and_counted(name):
    limiter = create(name, make_rule())
    assert limiter.active_keys() == 0
    for key in ("a", "b", "c"):
        limiter.decide(key, 0)
    assert limiter.active_keys() == 3
    assert limiter.state_entries() >= 3


@pytest.mark.parametrize("name", NAMES)
def test_retry_after_is_honest(name):
    """거절 때 받은 retry_after_ms만큼 기다렸다 보내면 받아들여지고, 1ms 먼저 보내면 거절된다."""
    for burst in (11, 15):
        early = create(name, make_rule())
        exact = create(name, make_rule())
        for limiter in (early, exact):
            for _ in range(burst - 1):
                limiter.decide("k", 0)
        retry = exact.decide("k", 0).retry_after_ms
        early.decide("k", 0)
        assert exact.decide("k", retry).accepted
        if retry > 1:
            assert not early.decide("k", retry - 1).accepted
