"""1.4 Fixed window counter: floor(t/W) 칸, 허용 건만 카운트, 경계 직전·직후."""

from rate_limit_lab.algorithms import FixedWindowCounter
from rate_limit_lab.models import Status
from tests.algo_helpers import KEY, max_in_any_window, send, statuses
from tests.conftest import make_rule


def counter(**overrides) -> FixedWindowCounter:
    return FixedWindowCounter(make_rule(**overrides))  # 기본: L=10, W=1000ms


def test_limit_within_one_window():
    verdicts = send(counter(), [0] * 12)
    assert statuses(verdicts) == ["allowed"] * 10 + ["rejected"] * 2
    assert verdicts[-1].retry_after_ms == 1000  # 다음 칸(1000ms)까지


def test_window_boundary_is_floor_t_over_w():
    limiter = counter()
    send(limiter, [0] * 10)
    assert limiter.decide(KEY, 999).status is Status.REJECTED  # 999ms: 아직 0번 칸
    assert limiter.decide(KEY, 999).retry_after_ms == 1
    assert limiter.decide(KEY, 1000).status is Status.ALLOWED  # 1000ms: 1번 칸, 카운터 0부터


def test_only_allowed_requests_are_counted():
    limiter = counter()
    send(limiter, [0] * 10 + [500] * 50)  # 거절 50건은 세지 않는다
    # 다음 칸은 완전히 새로 시작 → 10건 허용
    assert statuses(send(limiter, [1000] * 11)).count("allowed") == 10


def test_boundary_burst_allows_2l_in_moving_window():
    """창 직전(990ms) 10건 + 직후(1010ms) 10건 → 모두 허용. 이동 1초 안에 20건."""
    limiter = counter()
    times = [990] * 10 + [1010] * 10
    verdicts = send(limiter, times)
    assert statuses(verdicts) == ["allowed"] * 20
    allowed = [t for t, v in zip(times, verdicts) if v.accepted]
    assert max_in_any_window(allowed, 1000) == 20  # 한도 10의 두 배


def test_idle_recovery():
    limiter = counter()
    send(limiter, [0] * 10)
    assert statuses(send(limiter, [5500] * 11)).count("allowed") == 10
