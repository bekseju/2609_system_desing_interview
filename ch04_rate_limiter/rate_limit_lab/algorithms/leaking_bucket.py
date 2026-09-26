"""Leaking bucket (누출 버킷).

비유: 키마다 길이 Q(`queue_capacity`)인 줄(FIFO 큐)이 있고, 창구 직원 한 명이
일정한 간격(1000/r ms, r=`leak_per_sec`)마다 한 건씩 처리한다.
- 요청이 오면 줄이 꽉 찼는지 본다. 자리가 있으면 줄에 세우고(`queued`), 없으면 거절(`rejected`).
- 줄에 선 요청은 나중에 차례가 되면 처리된다(`processed`). **접수와 처리는 다른 시각이다.**

처리 시각 규칙(한 건 처리에 한 간격이 걸린다고 본다):
    처리 완료 시각 = max(도착 시각, 앞 요청의 처리 완료 시각) + 간격
따라서 처리 완료는 최소 한 간격씩 떨어지고, 처리 속도는 초당 r건을 넘지 않는다.
처리 완료 시각이 된 요청은 줄에서 빠진다(같은 시각에 도착한 요청은 빠진 뒤의 줄을 본다).

기본 정책(Q=10, r=10/s → 간격 100ms) 예:
  0ms에 15건 → 10건 접수(처리 100, 200, …, 1000ms), 5건 거절(retry_after 100ms)
  250ms 시점 줄 길이 = 8 (100ms, 200ms 건은 처리 완료)

이산 시간 해상도: 간격은 정수 ms로 반올림한다(`drain_interval_ms`). 1000/r가 정수가 아니면
건당 최대 0.5ms의 오차가 있다. 기본 정책은 정확히 100ms라 오차가 없다.

시뮬레이터용 추가 인터페이스:
- `admit(key, now_ms, item_id)`: `decide`와 같지만 요청 ID를 붙여 처리 이벤트와 연결한다.
- `drain(now_ms)`: now_ms까지 처리 완료된 이벤트 목록을 돌려준다.
- `flush()`: 줄에 남은 요청을 모두 처리한 것으로 보고 이벤트를 돌려준다(실행 종료 시).
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass

from rate_limit_lab.algorithms.base import RateLimiter, Verdict
from rate_limit_lab.models import Rule, Status


@dataclass(frozen=True, slots=True)
class ProcessedEvent:
    key: str
    item_id: str
    admitted_at_ms: int
    processed_at_ms: int

    @property
    def wait_ms(self) -> int:
        """접수부터 처리 완료까지 걸린 시간(큐 대기 + 처리 한 간격)."""
        return self.processed_at_ms - self.admitted_at_ms


@dataclass(slots=True)
class _Queue:
    pending: deque[int]  # 아직 처리되지 않은 요청들의 처리 완료 예정 시각 (오름차순)
    last_processed_ms: int | None  # 가장 마지막 요청의 처리 완료(예정) 시각
    last_seen_ms: int


class LeakingBucket(RateLimiter):
    name = "leaking_bucket"

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.capacity = rule.queue_capacity
        self.drain_interval_ms = max(1, round(1000 / rule.leak_per_sec))
        # 모든 키의 대기 요청을 처리 시각 순으로 담은 힙: (처리 시각, 접수 순번, 키, item_id, 접수 시각)
        self._heap: list[tuple[int, int, str, str, int]] = []
        self._seq = 0
        self._outbox: list[ProcessedEvent] = []  # 처리 완료됐지만 아직 drain으로 꺼내지 않은 이벤트

    # --- 공개 인터페이스 -------------------------------------------------------

    def admit(self, key: str, now_ms: int, item_id: str | None = None) -> Verdict:
        self._advance(now_ms)
        return self._admit(key, now_ms, item_id)

    def drain(self, now_ms: int) -> list[ProcessedEvent]:
        self._advance(now_ms)
        events, self._outbox = self._outbox, []
        return events

    def flush(self) -> list[ProcessedEvent]:
        last = max((entry[0] for entry in self._heap), default=self._now_ms or 0)
        return self.drain(max(last, self._now_ms or 0))

    def next_due_ms(self) -> int | None:
        """가장 먼저 처리 완료될 요청의 예정 시각 (대기 요청이 없으면 None). HTTP 작업자용."""
        return self._heap[0][0] if self._heap else None

    def pending_events(self) -> bool:
        """처리 완료됐지만 아직 drain으로 꺼내지 않은 이벤트가 있으면 True."""
        return bool(self._outbox)

    def queue_length(self, key: str) -> int:
        state = self._states.get(key)
        return len(state.pending) if state else 0

    def state_entries(self) -> int:
        return len(self._heap)

    # --- 내부 ------------------------------------------------------------------

    def _decide(self, key: str, now_ms: int) -> Verdict:
        return self._admit(key, now_ms, None)

    def _admit(self, key: str, now_ms: int, item_id: str | None) -> Verdict:
        queue = self._states.get(key)
        if queue is None:
            queue = self._states[key] = _Queue(pending=deque(), last_processed_ms=None, last_seen_ms=now_ms)
        queue.last_seen_ms = now_ms

        if len(queue.pending) >= self.capacity:
            # 맨 앞 요청이 처리되어 줄에서 빠지는 시각에 자리가 난다.
            return Verdict(Status.REJECTED, 0, queue.pending[0] - now_ms, "queue_full")

        start_ms = now_ms if queue.last_processed_ms is None else max(now_ms, queue.last_processed_ms)
        processed_at = start_ms + self.drain_interval_ms
        queue.pending.append(processed_at)
        queue.last_processed_ms = processed_at

        self._seq += 1
        item = item_id if item_id is not None else f"{key}#{self._seq}"
        heapq.heappush(self._heap, (processed_at, self._seq, key, item, now_ms))
        return Verdict(Status.QUEUED, self.capacity - len(queue.pending), None, "enqueued")

    def _advance(self, now_ms: int) -> None:
        super()._advance(now_ms)
        # now_ms까지 처리 완료 시각이 된 요청을 줄에서 빼고 이벤트로 남긴다.
        while self._heap and self._heap[0][0] <= now_ms:
            processed_at, _, key, item, admitted_at = heapq.heappop(self._heap)
            self._states[key].pending.popleft()
            self._outbox.append(ProcessedEvent(key, item, admitted_at, processed_at))

    def _is_fresh(self, queue: _Queue, now_ms: int) -> bool:
        # 줄이 비었으면(모두 처리 완료) 처음 본 키와 같다.
        return not queue.pending
