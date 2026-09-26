"""결정적 재생기: 저장된 요청 CSV를 가상 시계로 알고리즘에 주입한다.

    result = replay(requests, rules, "token_bucket")
    result.decisions   # 요청마다 최종 판정(Decision), 재생 순서
    result.events      # 시간순 이벤트: arrival / decision / processed
    result.stats       # 상태별 건수, 활성 키 수, 이동 창 최대 허용 수 등

재생 규칙:
- 요청은 `replay_order`(시각, 같은 시각이면 request_id) 순서로 넣는다.
- 가상 시계에서 도착 = 판정 시각 = `scheduled_at_ms`. 판정 지연(`latency_us`)만 실제로 잰다.
  (`measure_latency=False`면 0으로 두어 출력 파일이 완전히 같아진다.)
- 누출 버킷은 접수(`queued`) 뒤 처리 시각이 되면 `processed` 이벤트를 남기고, 최종 상태는
  `processed`가 된다. 재생이 끝나면 줄에 남은 요청을 모두 처리(flush)한다.
- `evict_interval_ms`마다 유휴 키 제거를 돌린다. 제거는 판정을 바꾸지 않는다(1.8에서 검증).
- 같은 시각에서는 [그 시각까지 처리 완료된 이벤트] → [도착] → [판정] 순서로 기록한다.
"""

from __future__ import annotations

import csv
import json
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rate_limit_lab.algorithms import LeakingBucket, RateLimiter, create
from rate_limit_lab.clock import VirtualClock, replay_order
from rate_limit_lab.keys import make_key, select_rules
from rate_limit_lab.metrics.windows import max_per_key_in_moving_window
from rate_limit_lab.models import Decision, Request, Rule, Status, write_decisions


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    at_ms: int
    request_id: str
    event: str  # arrival | decision | processed
    status: str  # decision: allowed/rejected/queued, processed: processed, arrival: 빈 문자열


EVENT_FIELDS = ("at_ms", "request_id", "event", "status")


@dataclass
class ReplayResult:
    algorithm: str
    decisions: list[Decision]
    events: list[ReplayEvent]
    stats: dict[str, Any]
    keys: list[str]  # decisions와 같은 순서의 정책 키


@dataclass
class _Pending:
    request: Request
    key: str
    verdict: Any
    latency_us: float
    processed_at_ms: int | None = None


def replay(
    requests: Iterable[Request],
    rules: Mapping[str, Rule],
    algorithm: str,
    *,
    measure_latency: bool = True,
    evict_interval_ms: int | None = 1000,
) -> ReplayResult:
    ordered = replay_order(requests)
    clock = VirtualClock()
    limiters: dict[str, RateLimiter] = {}  # rule_id -> 알고리즘 인스턴스
    records: dict[str, _Pending] = {}
    events: list[ReplayEvent] = []
    next_evict = evict_interval_ms
    evicted = peak_keys = 0

    def collect_processed(now_ms: int) -> None:
        for limiter in limiters.values():
            if isinstance(limiter, LeakingBucket):
                for e in limiter.drain(now_ms):
                    records[e.item_id].processed_at_ms = e.processed_at_ms
                    events.append(ReplayEvent(e.processed_at_ms, e.item_id, "processed", Status.PROCESSED.value))

    for request in ordered:
        now = clock.advance_to(request.scheduled_at_ms)

        # 1) 유휴 키 제거 (정해진 간격마다)
        while next_evict is not None and next_evict <= now:
            for limiter in limiters.values():
                evicted += limiter.evict_idle(next_evict)
            next_evict += evict_interval_ms  # type: ignore[operator]

        # 2) 지금까지 처리 완료된 누출 버킷 요청
        collect_processed(now)

        # 3) 판정
        rule_list = select_rules(request, rules)
        if len(rule_list) != 1:
            raise NotImplementedError("복수 규칙 결합은 7단계에서 구현한다")
        rule = rule_list[0]
        limiter = limiters.get(rule.rule_id)
        if limiter is None:
            limiter = limiters[rule.rule_id] = create(algorithm, rule)
        key = make_key(rule, request)

        events.append(ReplayEvent(now, request.request_id, "arrival", ""))
        started = time.perf_counter_ns()
        if isinstance(limiter, LeakingBucket):
            verdict = limiter.admit(key, now, request.request_id)
        else:
            verdict = limiter.decide(key, now)
        elapsed_us = (time.perf_counter_ns() - started) / 1000 if measure_latency else 0.0
        events.append(ReplayEvent(now, request.request_id, "decision", verdict.status.value))
        records[request.request_id] = _Pending(request, key, verdict, elapsed_us)
        peak_keys = max(peak_keys, sum(l.active_keys() for l in limiters.values()))

    # 4) 남은 대기 요청 처리
    for limiter in limiters.values():
        if isinstance(limiter, LeakingBucket):
            for e in limiter.flush():
                records[e.item_id].processed_at_ms = e.processed_at_ms
                events.append(ReplayEvent(e.processed_at_ms, e.item_id, "processed", Status.PROCESSED.value))

    decisions = [_to_decision(records[r.request_id]) for r in ordered]

    # 5) 통계 (마지막에 한 번 더 제거해 제거 전후 값을 남긴다)
    end_ms = max([clock.now_ms()] + [l.now_ms or 0 for l in limiters.values()])  # flush로 시각이 더 갔을 수 있다
    keys_before = sum(l.active_keys() for l in limiters.values())
    entries_before = sum(l.state_entries() for l in limiters.values())
    final_evicted = sum(l.evict_idle(end_ms) for l in limiters.values())
    stats = _stats(algorithm, rules, records, decisions)
    stats.update({
        "active_keys_peak": peak_keys,
        "evicted_during_run": evicted,
        "active_keys_end_before_evict": keys_before,
        "state_entries_end_before_evict": entries_before,
        "evicted_at_end": final_evicted,
        "active_keys_end_after_evict": sum(l.active_keys() for l in limiters.values()),
        "state_entries_end_after_evict": sum(l.state_entries() for l in limiters.values()),
        "end_ms": end_ms,
    })
    return ReplayResult(algorithm, decisions, events, stats, [records[r.request_id].key for r in ordered])


def _to_decision(p: _Pending) -> Decision:
    v = p.verdict
    processed = p.processed_at_ms is not None
    return Decision(
        request_id=p.request.request_id,
        arrival_at_ms=p.request.scheduled_at_ms,
        decision_at_ms=p.request.scheduled_at_ms,
        status=Status.PROCESSED if processed else v.status,
        processed_at_ms=p.processed_at_ms,
        retry_after_ms=v.retry_after_ms,
        remaining=v.remaining,
        latency_us=p.latency_us,
        reason="drained" if processed else v.reason,
    )


def _stats(algorithm: str, rules: Mapping[str, Rule], records: dict[str, _Pending],
           decisions: list[Decision]) -> dict[str, Any]:
    by_status = Counter(d.status.value for d in decisions)
    window_ms = min(rule.window_ms for rule in rules.values())
    accepted = [(records[d.request_id].key, d.decision_at_ms) for d in decisions if d.status is not Status.REJECTED]
    processed = [(records[d.request_id].key, d.processed_at_ms) for d in decisions if d.processed_at_ms is not None]
    max_accepted, max_key = max_per_key_in_moving_window(accepted, window_ms)
    max_processed, _ = max_per_key_in_moving_window(processed, window_ms)
    return {
        "algorithm": algorithm,
        "input_requests": len(decisions),
        "status_counts": {s.value: by_status.get(s.value, 0) for s in Status},
        "accepted": len(accepted),
        "rejected": by_status.get(Status.REJECTED.value, 0),
        "moving_window_ms": window_ms,
        "max_accepted_per_key_in_moving_window": max_accepted,
        "max_accepted_key": max_key,
        "max_processed_per_key_in_moving_window": max_processed if processed else None,
    }


# ---------------------------------------------------------------------------
# 파일 출력


def write_events(path: Path, events: Iterable[ReplayEvent]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(EVENT_FIELDS)
        for e in events:
            writer.writerow(asdict(e).values())


def write_result(out_dir: Path, result: ReplayResult) -> None:
    out_dir = Path(out_dir)
    write_decisions(out_dir / "decisions.csv", result.decisions)
    write_events(out_dir / "events.csv", result.events)
    (out_dir / "summary.json").write_text(
        json.dumps(result.stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
