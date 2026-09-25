"""1.5 Sliding window log strict 모드 / 1.6 PDF 재현 모드."""

import random

import pytest

from rate_limit_lab.algorithms import SlidingWindowLog, SlidingWindowLogPdf
from rate_limit_lab.models import Status
from tests.algo_helpers import KEY, max_in_any_window, send, statuses
from tests.conftest import make_rule


def strict(**overrides) -> SlidingWindowLog:
    return SlidingWindowLog(make_rule(**overrides))  # 기본: L=10, W=1000ms


def pdf(**overrides) -> SlidingWindowLogPdf:
    return SlidingWindowLogPdf(make_rule(**overrides))


# --- 1.5 strict --------------------------------------------------------------


def test_strict_expiry_is_timestamp_le_t_minus_w():
    limiter = strict()
    send(limiter, [0] * 10)
    # 999ms: 0ms 기록은 아직 창 안 (0 > 999-1000) → 거절, 1ms 뒤 가능
    v = limiter.decide(KEY, 999)
    assert v.status is Status.REJECTED and v.retry_after_ms == 1
    # 1000ms: 0 <= 1000-1000 이므로 제거 → 10건 모두 자리 생김
    assert statuses(send(limiter, [1000] * 11)).count("allowed") == 10


def test_strict_stores_only_allowed():
    limiter = strict()
    send(limiter, [0] * 10 + [500] * 100)
    assert limiter.state_entries() == 10  # 거절 100건은 기록하지 않는다


def test_strict_boundary_burst_rejects_second_half():
    # 고정 윈도와 달리 990ms 10건 뒤 1010ms 요청은 모두 거절
    verdicts = send(strict(), [990] * 10 + [1010] * 10)
    assert statuses(verdicts) == ["allowed"] * 10 + ["rejected"] * 10
    assert verdicts[-1].retry_after_ms == 980  # 990ms 기록이 1990ms에 창 밖으로


@pytest.mark.parametrize("seed", range(10))
def test_strict_never_exceeds_limit_in_any_moving_window(seed):
    rng = random.Random(seed)
    times = sorted(rng.randrange(0, 20_000) for _ in range(2_000))  # 평균 100 req/s, 몰림 포함
    times += [t + 1000 for t in range(20_000, 20_010)] + [21_999] * 30  # 경계 몰림 추가
    limiter = strict()
    allowed = [t for t, v in zip(times, send(limiter, times)) if v.accepted]
    assert max_in_any_window(allowed, 1000) <= 10
    assert max_in_any_window(allowed, 1000) == 10  # 한도까지는 쓴다


def test_strict_idle_recovery():
    limiter = strict()
    send(limiter, [0] * 10)
    assert statuses(send(limiter, [5000] * 11)).count("allowed") == 10


# --- 1.6 PDF 재현 모드 ---------------------------------------------------------


def test_pdf_records_rejected_timestamps():
    limiter = pdf()
    send(limiter, [0] * 10 + [500] * 100)
    assert limiter.state_entries() == 110  # 거절 100건도 기록 → 로그가 커진다


def test_pdf_book_example_rejections_block_later_requests():
    """책 예시와 같은 흐름: 거절된 기록이 창 안에 남아 뒤의 요청까지 막는다."""
    # 0ms 10건 허용, 500ms 1건 거절(기록됨). 1000ms에 0ms 기록이 빠져도 500ms 기록이 남는다.
    s, p = strict(), pdf()
    times = [0] * 10 + [500] + [1000] * 10
    assert statuses(send(s, times)).count("allowed") == 20  # strict: 1000ms에 10건 허용
    assert statuses(send(p, times)).count("allowed") == 19  # PDF: 500ms 거절 기록 때문에 1건 덜 허용


def test_pdf_vs_strict_under_sustained_overload():
    """초당 20건을 5초간: strict는 초당 10건씩 계속 허용, PDF는 첫 10건 이후 전부 거절."""
    times = list(range(0, 5000, 50))
    s, p = strict(), pdf()
    strict_allowed = [t for t, v in zip(times, send(s, times)) if v.accepted]
    pdf_allowed = [t for t, v in zip(times, send(p, times)) if v.accepted]
    assert len(strict_allowed) == 50
    assert len(pdf_allowed) == 10 and max(pdf_allowed) < 1000
    # 로그 크기: strict는 최대 L, PDF는 최근 1초 동안의 모든 요청(20건)
    assert s.state_entries() == 10
    assert p.state_entries() == 20


def test_pdf_recovers_later_than_strict_after_overload():
    """0~2950ms 동안 초당 20건 과부하 후, 천천히 보낸 요청의 판정 비교."""
    overload = list(range(0, 3000, 50))
    s, p = strict(), pdf()
    send(s, overload)
    send(p, overload)
    # strict: 최근 1초의 '허용' 기록은 9건(2050~2450ms) → 3000ms에 바로 허용
    # PDF: 최근 1초의 '모든' 기록이 19건(2050~2950ms) → 거절
    assert statuses(send(s, [3000, 3500, 3600])) == ["allowed"] * 3
    # PDF 3500ms: 과부하 기록 9건(2550~2950) + 3000ms 거절 기록 = 10건 → 여전히 거절
    # PDF 3600ms: 과부하 기록 7건(2650~2950) + 3000·3500ms 기록 = 9건 → 허용
    assert statuses(send(p, [3000, 3500, 3600])) == ["rejected", "rejected", "allowed"]
