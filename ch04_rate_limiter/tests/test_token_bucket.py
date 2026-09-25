"""1.2 Token bucket: 초기 토큰 C, 연속 재충전, 용량 상한, 허용 시 1개 소비."""

from rate_limit_lab.algorithms import TokenBucket
from rate_limit_lab.models import Status
from tests.algo_helpers import KEY, send, statuses
from tests.conftest import make_rule


def bucket(**overrides) -> TokenBucket:
    return TokenBucket(make_rule(**overrides))  # 기본: C=10, r=10/s


def test_initial_burst_up_to_capacity():
    # 처음에는 토큰 10개가 가득 → 0ms에 15건이면 10건 허용, 5건 거절
    verdicts = send(bucket(), [0] * 15)
    assert statuses(verdicts) == ["allowed"] * 10 + ["rejected"] * 5
    assert [v.remaining for v in verdicts[:10]] == [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]
    assert all(v.retry_after_ms == 100 for v in verdicts[10:])  # 토큰 1개가 차는 데 100ms


def test_rejection_does_not_consume_tokens():
    limiter = bucket()
    send(limiter, [0] * 10)  # 토큰 0개
    send(limiter, [50] * 100)  # 50ms에 100건 거절 — 토큰을 빼지 않아야 한다
    # 100ms에는 거절 여부와 관계없이 딱 1개가 차 있어야 한다
    assert statuses(send(limiter, [100, 100])) == ["allowed", "rejected"]


def test_continuous_refill_fraction():
    limiter = bucket()
    send(limiter, [0] * 10)
    # 50ms 뒤 토큰 0.5개 → 거절, 50ms 더 필요
    v = limiter.decide(KEY, 50)
    assert v.status is Status.REJECTED and v.retry_after_ms == 50
    # 100ms: 1.0개 → 허용
    assert limiter.decide(KEY, 100).status is Status.ALLOWED


def test_refill_capped_at_capacity_after_idle():
    limiter = bucket()
    send(limiter, [0] * 10)
    # 1초 쉬면 10개로 회복, 1시간을 쉬어도 10개를 넘지 않는다
    assert statuses(send(limiter, [1000] * 11)).count("allowed") == 10
    assert statuses(send(limiter, [3_600_000] * 11)).count("allowed") == 10


def test_partial_recovery_after_idle():
    limiter = bucket()
    send(limiter, [0] * 10)
    # 350ms 쉬면 3.5개 → 3건 허용
    assert statuses(send(limiter, [350] * 5)).count("allowed") == 3


def test_steady_rate_equals_refill_rate():
    # 버스트를 다 쓴 뒤 20 req/s로 보내면, 장기적으로 초당 10건(재충전 속도)만 허용된다
    limiter = bucket()
    times = list(range(0, 10_000, 50))
    allowed = [t for t, v in zip(times, send(limiter, times)) if v.accepted]
    assert len(allowed) == 10 + 99  # 초기 10개 + 50~9950ms 동안 재충전된 99개 (9.95초 × 10)
    after_burst = [t for t in allowed if t >= 1000]
    assert all(b - a == 100 for a, b in zip(after_burst, after_burst[1:]))


def test_fractional_rate_and_capacity():
    limiter = bucket(bucket_capacity=2, refill_per_sec=0.5)  # 2초에 토큰 1개
    assert statuses(send(limiter, [0, 0, 0])) == ["allowed", "allowed", "rejected"]
    assert limiter.decide(KEY, 1999).retry_after_ms == 1
    assert limiter.decide(KEY, 2000).status is Status.ALLOWED
