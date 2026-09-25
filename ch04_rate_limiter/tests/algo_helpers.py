"""알고리즘 테스트 공용 도우미."""

from __future__ import annotations

from collections.abc import Iterable

from rate_limit_lab.algorithms import RateLimiter, Verdict
from rate_limit_lab.models import Status

KEY = "rl:user_default:user:alice"


def send(limiter: RateLimiter, times: Iterable[int], key: str = KEY) -> list[Verdict]:
    """times의 각 시각에 요청 1건씩 보내고 판정 목록을 돌려준다."""
    return [limiter.decide(key, t) for t in times]


def statuses(verdicts: Iterable[Verdict]) -> list[str]:
    return [v.status.value for v in verdicts]


def accepted_count(verdicts: Iterable[Verdict]) -> int:
    return sum(v.accepted for v in verdicts)


def accepted_times(limiter: RateLimiter, times: Iterable[int], key: str = KEY) -> list[int]:
    return [t for t in times if limiter.decide(key, t).status is not Status.REJECTED]


def max_in_any_window(times: list[int], window_ms: int) -> int:
    """정렬된 시각 목록에서, 길이 W인 어떤 반열린 구간 [s, s+W)에 들어가는 최대 건수."""
    best, left = 0, 0
    for right, t in enumerate(times):
        while times[left] <= t - window_ms:
            left += 1
        best = max(best, right - left + 1)
    return best
