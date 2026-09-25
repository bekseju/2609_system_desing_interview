"""Sliding window log (이동 윈도 로그).

비유: 키마다 요청 시각을 적어 두는 수첩(로그)이 있다.
- 새 요청이 시각 t에 오면, 먼저 W(`window_ms`)보다 오래된 기록(시각 <= t - W)을 지운다.
  남은 기록은 최근 W 동안, 즉 (t-W, t] 구간의 요청이다.
- 남은 기록이 L(`limit`)건 미만이면 허용한다.

두 가지 모드 (명세 4절):
- strict 모드 (`sliding_log`): **허용한 요청만** 적는다.
  → 어떤 1초 구간을 잡아도 허용 건수가 L을 넘지 않는다(가장 정확).
- PDF 재현 모드 (`sliding_log_pdf`): 책 설명처럼 **거절된 요청도** 적는다.
  → 계속 초과 요청을 보내면 거절 기록이 쌓여 수첩이 커지고(메모리 증가),
    한도 안으로 줄여 보내도 한동안 계속 거절될 수 있다.

기본 정책(L=10, W=1000ms), 50ms마다 요청(초당 20건)을 계속 보내면:
  strict: 처음 10건 허용 후, 이후 1초마다 약 10건씩 계속 허용
  PDF   : 처음 10건 허용 후, 수첩에 늘 L건 이상이 남아 있어 전부 거절
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from rate_limit_lab.algorithms.base import RateLimiter, Verdict
from rate_limit_lab.models import Rule, Status


@dataclass(slots=True)
class _Log:
    times: deque[int]  # 기록된 요청 시각 (오름차순)
    last_seen_ms: int


class SlidingWindowLog(RateLimiter):
    name = "sliding_log"

    def __init__(self, rule: Rule, *, record_rejected: bool = False) -> None:
        super().__init__(rule)
        self.limit = rule.limit
        self.window_ms = rule.window_ms
        self.record_rejected = record_rejected
        self.name = "sliding_log_pdf" if record_rejected else "sliding_log"

    def _decide(self, key: str, now_ms: int) -> Verdict:
        log = self._states.get(key)
        if log is None:
            log = self._states[key] = _Log(times=deque(), last_seen_ms=now_ms)
        log.last_seen_ms = now_ms
        times = log.times

        # 1) 창 밖으로 나간 기록을 지운다: 시각 <= t - W
        while times and times[0] <= now_ms - self.window_ms:
            times.popleft()

        # 2) 남은 기록이 L건 미만이면 허용.
        allowed = len(times) < self.limit
        if allowed or self.record_rejected:
            times.append(now_ms)

        if allowed:
            return Verdict(Status.ALLOWED, max(0, self.limit - len(times)), None, "within_log_limit")

        # 거절: 수첩의 기록이 L건 미만으로 줄어드는 시각까지 기다린다.
        # 오래된 쪽에서 (len - L + 1)번째 기록이 창 밖으로 나가면 L-1건이 남는다.
        freeing_time = times[len(times) - self.limit]
        return Verdict(Status.REJECTED, 0, freeing_time + self.window_ms - now_ms, "log_limit_reached")

    def state_entries(self) -> int:
        return sum(len(log.times) for log in self._states.values())

    def _is_fresh(self, log: _Log, now_ms: int) -> bool:
        # 모든 기록이 창 밖이면 수첩이 빈 것과 같다.
        return not log.times or log.times[-1] <= now_ms - self.window_ms


class SlidingWindowLogPdf(SlidingWindowLog):
    """PDF 재현 모드: 거절된 요청 시각도 로그에 남긴다."""

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule, record_rejected=True)
