"""Sliding window counter (이동 윈도 카운터).

고정 윈도의 경계 문제를 줄이면서 로그처럼 시각을 다 저장하지 않으려는 **근사** 방식이다.
키마다 "지금 칸(curr)"과 "바로 앞 칸(prev)"의 허용 건수 두 개만 저장한다.

시각 t가 지금 칸의 앞에서 offset ms 지점일 때, 최근 W 동안의 요청 수를 이렇게 추정한다.
    추정치 = curr + prev × (1 - offset / W)
"앞 칸의 요청이 칸 안에 고르게 퍼져 있었다"고 가정하고, 최근 W 구간에 걸치는 비율만큼만 센다.
추정치(반올림 전 실수)가 L 미만이면 허용하고, 허용한 건만 curr에 더한다.

책(PDF) 예: 한도 7/분, 앞 칸 5건, 지금 칸 3건, 지금 칸의 30% 지점
    추정치 = 3 + 5 × 0.7 = 6.5 < 7 → 허용

기본 정책(L=10, W=1000ms) 예:
  990ms에 10건 허용 후 1010ms: 추정치 = 0 + 10 × 0.99 = 9.9 < 10 → 1건 허용, 이후 거절
  (정확한 로그 방식이면 1010ms는 전부 거절 → 이 1건이 근사로 인한 false allow)

구현 메모: 실수 오차를 피하려고 비교는 양변에 W를 곱한 정수로 한다.
    curr×W + prev×(W - offset) < L×W
"""

from __future__ import annotations

from dataclasses import dataclass

from rate_limit_lab.algorithms.base import RateLimiter, Verdict
from rate_limit_lab.models import Rule, Status


@dataclass(slots=True)
class _Counter:
    index: int  # 지금 칸 번호 floor(t / W)
    curr: int  # 지금 칸에서 허용한 건수
    prev: int  # 바로 앞 칸에서 허용한 건수
    last_seen_ms: int


class SlidingWindowCounter(RateLimiter):
    name = "sliding_counter"

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.limit = rule.limit
        self.window_ms = rule.window_ms

    def estimate(self, key: str, now_ms: int) -> float:
        """현재 추정치(보고서·디버깅용, 상태를 바꾸지 않는다)."""
        counter = self._states.get(key)
        if counter is None:
            return 0.0
        curr, prev = self._counts_at(counter, now_ms // self.window_ms)
        offset = now_ms % self.window_ms
        return curr + prev * (1 - offset / self.window_ms)

    def _counts_at(self, counter: _Counter, index: int) -> tuple[int, int]:
        """칸 번호 index 기준의 (curr, prev)."""
        if index == counter.index:
            return counter.curr, counter.prev
        if index == counter.index + 1:
            return 0, counter.curr  # 한 칸 넘어감: 지금 칸이 앞 칸이 된다
        return 0, 0  # 두 칸 이상 지나감: 둘 다 0

    def _decide(self, key: str, now_ms: int) -> Verdict:
        W, L = self.window_ms, self.limit
        index, offset = divmod(now_ms, W)

        counter = self._states.get(key)
        if counter is None:
            counter = self._states[key] = _Counter(index=index, curr=0, prev=0, last_seen_ms=now_ms)
        counter.curr, counter.prev = self._counts_at(counter, index)
        counter.index = index
        counter.last_seen_ms = now_ms

        # 추정치 × W (정수)
        scaled = counter.curr * W + counter.prev * (W - offset)
        if scaled < L * W:
            counter.curr += 1
            scaled += W
            # 지금 바로 몇 건 더 허용될지: 추정치 + k < L 을 만족하는 k(0 이상)의 개수
            remaining = max(0, -(-(L * W - scaled) // W))
            return Verdict(Status.ALLOWED, remaining, None, "within_estimate")

        return Verdict(Status.REJECTED, 0, self._retry_after(counter, index, offset), "estimate_limit_reached")

    def _retry_after(self, counter: _Counter, index: int, offset: int) -> int:
        """추정치가 L 미만으로 내려가는 가장 이른 시각까지 남은 ms."""
        W, L = self.window_ms, self.limit
        curr, prev = counter.curr, counter.prev
        now_ms = index * W + offset
        # (a) 지금 칸 안에서: 앞 칸 가중치가 줄어 curr×W + prev×(W - o) < L×W 가 되는 첫 offset o
        if prev > 0:
            first_offset = (curr + prev - L) * W // prev + 1
            if first_offset < W:
                return index * W + first_offset - now_ms
        # (b) 다음 칸 시작: 추정치 = curr(앞 칸이 된 지금 칸, 가중치 1). curr < L이면 바로 허용.
        next_start = (index + 1) * W
        if curr < L:
            return next_start - now_ms
        # (c) curr == L이면 다음 칸 시작 1ms 뒤부터 가중치가 1 미만이 되어 허용된다.
        return next_start + 1 - now_ms

    def _is_fresh(self, counter: _Counter, now_ms: int) -> bool:
        # 두 칸 이상 지나면 curr, prev가 모두 0이 된다.
        return now_ms // self.window_ms >= counter.index + 2
