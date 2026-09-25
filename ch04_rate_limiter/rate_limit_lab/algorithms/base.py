"""모든 알고리즘이 따르는 공통 인터페이스.

사용법은 모든 알고리즘이 같다::

    limiter = TokenBucket(rule)
    verdict = limiter.decide("rl:user_default:user:alice", now_ms=990)
    verdict.status          # allowed / rejected (누출 버킷은 queued / rejected)
    verdict.remaining       # 지금 바로 더 보낼 수 있는 요청 수 (추정일 수 있음)
    verdict.retry_after_ms  # 거절 시, 다시 시도하면 받아들여지는 가장 이른 시점까지 남은 ms
    verdict.reason          # 판정 이유 (짧은 영문 코드)

계약:
- 시간(`now_ms`)은 정수 ms이며 되돌아가면 `ClockError`. 같은 시각은 허용한다.
- 알고리즘은 키마다 상태를 따로 가진다(키가 다르면 서로 영향이 없다).
- `evict_idle(now_ms)`는 유휴 키의 상태를 지운다. 단, **지워도 이후 판정이 바뀌지 않는
  키만** 지운다. 즉 `idle_ttl_ms` 이상 요청이 없었고, 상태가 "처음 본 키"와 같아진 키다.
  (예: 토큰이 가득 찬 버킷, 이미 끝난 창). 그래서 제거는 메모리만 줄이고 결과는 바꾸지 않는다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from rate_limit_lab.clock import ClockError
from rate_limit_lab.models import Rule, Status


@dataclass(frozen=True, slots=True)
class Verdict:
    status: Status
    remaining: int | None
    retry_after_ms: int | None
    reason: str

    @property
    def accepted(self) -> bool:
        """허용(allowed) 또는 큐 접수(queued)면 True."""
        return self.status is not Status.REJECTED


class RateLimiter(ABC):
    name: str = ""

    def __init__(self, rule: Rule) -> None:
        self.rule = rule
        self._states: dict[str, Any] = {}  # 키 -> 알고리즘별 상태 (각 상태는 last_seen_ms를 가진다)
        self._now_ms: int | None = None

    # --- 공개 인터페이스 -------------------------------------------------------

    def decide(self, key: str, now_ms: int) -> Verdict:
        self._advance(now_ms)
        return self._decide(key, now_ms)

    def evict_idle(self, now_ms: int) -> int:
        """지워도 판정이 바뀌지 않는 유휴 키를 지우고, 지운 키 수를 돌려준다."""
        self._advance(now_ms)
        ttl = self.rule.idle_ttl_ms
        expired = [
            key
            for key, state in self._states.items()
            if now_ms - state.last_seen_ms >= ttl and self._is_fresh(state, now_ms)
        ]
        for key in expired:
            del self._states[key]
        return len(expired)

    @property
    def now_ms(self) -> int | None:
        """이 인스턴스가 마지막으로 본 시각 (아직 없으면 None)."""
        return self._now_ms

    def active_keys(self) -> int:
        return len(self._states)

    def state_entries(self) -> int:
        """저장 중인 항목 수. 기본은 키 수이며, 로그·큐 방식은 로그/큐 항목 수를 센다."""
        return len(self._states)

    # --- 알고리즘별 구현 -------------------------------------------------------

    @abstractmethod
    def _decide(self, key: str, now_ms: int) -> Verdict: ...

    @abstractmethod
    def _is_fresh(self, state: Any, now_ms: int) -> bool:
        """이 상태가 '처음 본 키'의 상태와 판정상 같으면 True."""

    # --- 내부 ------------------------------------------------------------------

    def _advance(self, now_ms: int) -> None:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int):
            raise ClockError(f"now_ms: 정수 ms가 필요합니다 (값: {now_ms!r})")
        if self._now_ms is not None and now_ms < self._now_ms:
            raise ClockError(f"시계 역행: 현재 {self._now_ms}ms, 요청 {now_ms}ms")
        self._now_ms = now_ms
