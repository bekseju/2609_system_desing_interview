"""시계와 시간 구간.

- `VirtualClock`: 결정적 재생용 정수 ms 가상 시계. 역행을 허용하지 않는다.
- `MonotonicClock`: 실제 실행용 단조 시계(`time.monotonic_ns` 기준, 생성 시각이 0ms).
- 구간은 모두 반열린 구간 ``[start, end)``이며 시뮬레이션 창 시작은 0ms이다.
- 같은 시각의 요청은 `request_id` 문자열 순서로 안정 정렬한다.
  (생성기는 정렬이 숫자 순서와 같도록 0으로 채운 ID를 쓴다.)
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from rate_limit_lab.models import Request


class ClockError(ValueError):
    """시계를 과거로 되돌리거나 정수 ms가 아닌 값을 쓸 때 발생한다."""


class Clock(Protocol):
    def now_ms(self) -> int: ...


def _check_ms(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClockError(f"{name}: 정수 ms가 필요합니다 (값: {value!r})")
    return value


class VirtualClock:
    def __init__(self, start_ms: int = 0) -> None:
        self._now_ms = _check_ms("start_ms", start_ms)

    def now_ms(self) -> int:
        return self._now_ms

    def advance_to(self, t_ms: int) -> int:
        _check_ms("t_ms", t_ms)
        if t_ms < self._now_ms:
            raise ClockError(f"시계 역행: 현재 {self._now_ms}ms, 요청 {t_ms}ms")
        self._now_ms = t_ms
        return t_ms

    def advance_by(self, delta_ms: int) -> int:
        _check_ms("delta_ms", delta_ms)
        return self.advance_to(self._now_ms + delta_ms)


class MonotonicClock:
    def __init__(self) -> None:
        self._origin_ns = time.monotonic_ns()

    def now_ns(self) -> int:
        return time.monotonic_ns() - self._origin_ns

    def now_ms(self) -> int:
        return self.now_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class Interval:
    """반열린 구간 ``[start_ms, end_ms)``."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        _check_ms("start_ms", self.start_ms)
        _check_ms("end_ms", self.end_ms)
        if self.end_ms <= self.start_ms:
            raise ClockError(f"빈 구간: [{self.start_ms}, {self.end_ms})")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def contains(self, t_ms: int) -> bool:
        return self.start_ms <= t_ms < self.end_ms


def window_index(t_ms: int, width_ms: int) -> int:
    """``floor(t / W)``. t=W는 다음 창(인덱스 1)에 속한다."""
    _check_ms("t_ms", t_ms)
    if _check_ms("width_ms", width_ms) <= 0:
        raise ClockError(f"width_ms는 양수여야 합니다 (값: {width_ms})")
    return t_ms // width_ms


def bucket_of(t_ms: int, width_ms: int) -> Interval:
    """t가 속한 폭 W의 구간 ``[kW, (k+1)W)``."""
    start = window_index(t_ms, width_ms) * width_ms
    return Interval(start, start + width_ms)


def replay_sort_key(request: Request) -> tuple[int, str]:
    return (request.scheduled_at_ms, request.request_id)


def replay_order(requests: Iterable[Request]) -> list[Request]:
    """재생 순서: `scheduled_at_ms` 오름차순, 같은 시각은 `request_id` 순."""
    return sorted(requests, key=replay_sort_key)
