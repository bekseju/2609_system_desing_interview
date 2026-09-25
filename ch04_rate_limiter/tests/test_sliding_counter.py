"""1.7 Sliding window counter: curr + prev × (1 - offset/W), 반올림 전 비교."""

import pytest

from rate_limit_lab.algorithms import SlidingWindowCounter
from rate_limit_lab.models import Status
from tests.algo_helpers import KEY, send, statuses
from tests.conftest import make_rule


def counter(**overrides) -> SlidingWindowCounter:
    return SlidingWindowCounter(make_rule(**overrides))  # 기본: L=10, W=1000ms


def test_book_example_6_5_is_allowed():
    """책 예: 한도 7/분, 앞 칸 5건, 지금 칸 3건, 30% 지점 → 3 + 5×0.7 = 6.5 < 7 → 허용."""
    limiter = counter(limit=7, window_ms=60_000)
    send(limiter, [0] * 5)  # 앞 칸(0~60s) 5건
    assert statuses(send(limiter, [70_000] * 3)) == ["allowed"] * 3  # 지금 칸 3건
    assert limiter.estimate(KEY, 78_000) == pytest.approx(6.5)  # 30% 지점
    assert limiter.decide(KEY, 78_000).status is Status.ALLOWED
    assert limiter.estimate(KEY, 78_000) == pytest.approx(7.5)
    assert limiter.decide(KEY, 78_000).status is Status.REJECTED


def test_compares_before_rounding():
    # 추정치 9.9: 반올림하면 10이 되어 거절이겠지만, 실수 그대로 9.9 < 10 이므로 허용
    limiter = counter()
    send(limiter, [990] * 10)
    assert limiter.estimate(KEY, 1010) == pytest.approx(9.9)
    assert statuses(send(limiter, [1010] * 2)) == ["allowed", "rejected"]


def test_exact_equality_is_rejected():
    # 추정치가 정확히 10.0이면 10 < 10 이 아니므로 거절 (정수 비교로 오차 없음)
    limiter = counter()
    send(limiter, [0] * 10)  # 0번 칸 10건
    send(limiter, [1500] * 5)  # 1500ms: 0 + 10×0.5 = 5 → 5건 허용 → curr=5
    assert limiter.estimate(KEY, 1500) == pytest.approx(10.0)
    assert limiter.decide(KEY, 1500).status is Status.REJECTED


def test_previous_window_influence_declines_over_time():
    limiter = counter()
    send(limiter, [0] * 10)  # 앞 칸 10건
    # 시각마다 10건씩 보냈을 때 허용 건수 (curr는 누적된다)
    #  1000ms: 0 + 10×1.00 = 10    → 0건
    #  1250ms: 0 + 10×0.75 = 7.5   → 3건 (curr=3)
    #  1500ms: 3 + 10×0.50 = 8     → 2건 (curr=5)
    #  1750ms: 5 + 10×0.25 = 7.5   → 3건 (curr=8)
    #  1999ms: 8 + 10×0.001 = 8.01 → 2건 (curr=10)
    for t, expected in [(1000, 0), (1250, 3), (1500, 2), (1750, 3), (1999, 2)]:
        got = statuses(send(limiter, [t] * 10)).count("allowed")
        assert got == expected, (t, got)


def test_window_boundary_sequence():
    limiter = counter()
    send(limiter, [0] * 10)
    v = limiter.decide(KEY, 999)  # 같은 칸, curr=10 → 거절
    assert v.status is Status.REJECTED
    assert v.retry_after_ms == 2  # 1000ms에는 추정치 10(거절), 1001ms에 9.99 → 허용
    assert limiter.decide(KEY, 1000).status is Status.REJECTED
    assert limiter.decide(KEY, 1001).status is Status.ALLOWED


def test_idle_recovery_after_two_windows():
    limiter = counter()
    send(limiter, [0] * 10)
    # 두 칸 이상 지나면 앞 칸도 0 → 완전 회복
    assert statuses(send(limiter, [2000] * 11)).count("allowed") == 10


def test_only_allowed_are_counted():
    limiter = counter()
    send(limiter, [0] * 10 + [100] * 100)
    assert limiter.estimate(KEY, 999) == pytest.approx(10.0)


def test_remaining_is_based_on_estimate():
    limiter = counter()
    send(limiter, [990] * 10)
    v = limiter.decide(KEY, 1010)  # 추정 9.9 → 허용 후 10.9
    assert v.status is Status.ALLOWED and v.remaining == 0
    limiter2 = counter()
    v = limiter2.decide(KEY, 0)
    assert v.remaining == 9
