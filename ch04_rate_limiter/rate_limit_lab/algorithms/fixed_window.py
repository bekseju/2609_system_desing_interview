"""Fixed window counter (고정 윈도 카운터).

비유: 시간을 W(`window_ms`) 단위 칸으로 자른다. 0~999ms가 0번 칸, 1000~1999ms가 1번 칸.
- 요청 시각 t의 칸 번호 = floor(t / W).
- 칸마다 카운터가 있고, 칸 안에서 처음 L(`limit`)건만 허용한다. 허용한 건만 센다.
- 새 칸이 시작되면 카운터는 0부터 다시 센다.

약점: 칸 경계에 몰리면 1초 안에 2L건까지 허용될 수 있다.
  990ms에 10건(0번 칸) + 1010ms에 10건(1번 칸) → 20건 모두 허용.
  990~1010ms라는 20ms 사이에 20건이 통과한 셈이다.
"""

from __future__ import annotations

from dataclasses import dataclass

from rate_limit_lab.algorithms.base import RateLimiter, Verdict
from rate_limit_lab.models import Rule, Status


@dataclass(slots=True)
class _Window:
    index: int  # 칸 번호 floor(t / W)
    count: int  # 이 칸에서 허용한 건수
    last_seen_ms: int


class FixedWindowCounter(RateLimiter):
    name = "fixed_window"

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.limit = rule.limit
        self.window_ms = rule.window_ms

    def _decide(self, key: str, now_ms: int) -> Verdict:
        index = now_ms // self.window_ms
        window = self._states.get(key)
        if window is None or window.index != index:
            # 처음 본 키이거나 새 칸이 시작됐다 → 카운터를 0으로.
            window = self._states[key] = _Window(index=index, count=0, last_seen_ms=now_ms)
        window.last_seen_ms = now_ms

        if window.count < self.limit:
            window.count += 1
            return Verdict(Status.ALLOWED, self.limit - window.count, None, "within_window_limit")

        # 거절: 다음 칸이 시작될 때까지 기다려야 한다.
        next_window_ms = (index + 1) * self.window_ms
        return Verdict(Status.REJECTED, 0, next_window_ms - now_ms, "window_limit_reached")

    def _is_fresh(self, window: _Window, now_ms: int) -> bool:
        # 칸이 끝났으면 다음 요청은 어차피 새 카운터로 시작한다.
        return now_ms // self.window_ms != window.index
