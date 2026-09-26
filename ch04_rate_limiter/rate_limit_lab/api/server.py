"""HTTP rate limiter API 서버 (TODO 4.1, 4.2).

    python -m rate_limit_lab.api.server --algorithm token_bucket --port 8081 --instance-id A

엔드포인트
- GET /work?client_id=...[&ip=...][&rule_id=...]   요청 1건 판정
    200 허용 (설정한 --work-ms만큼 가상 작업 후 응답)
    202 누출 버킷 큐 접수. 처리는 나중에 비동기로 일어나며 /requests/{id}, /events로 조회
    429 거절 (누출 버킷은 큐가 가득 찬 경우)
    400 client_id 누락 등 잘못된 요청
- GET /requests/{request_id}   누출 버킷 요청의 접수·처리 상태 조회
- GET /events?after=N          처리 완료 이벤트 목록 (N번째 이후, 커서 방식)
- GET /stats                   건수·활성 키·판정 지연·RSS·시계 원천
- GET /health                  상태 점검

시계: 서버 시작 시각을 0으로 하는 단조 시계(ms, `MonotonicClock`). 모든 시각 필드가 이 기준이다.

응답 헤더 (가능한 범위에서, 알고리즘별 정확도)
| 헤더                           | 의미                                                                           |
| ------------------------------ | ------------------------------------------------------------------------------ |
| X-RateLimit-Limit              | 창 방식: limit / 토큰 버킷: 버킷 용량 / 누출 버킷: 큐 용량                     |
| X-RateLimit-Remaining          | 판정 직후 바로 더 보낼 수 있는 건수. 토큰 버킷은 남은 토큰의 내림, 슬라이딩    |
|                                | 카운터는 **추정치 기반 근사**, 누출 버킷은 큐 빈자리 수                        |
| Retry-After                    | 429에만. 초 단위 정수 = ceil(retry_after_ms / 1000), 최소 1 (HTTP 규격상 초)   |
| X-RateLimit-Retry-After-Ms     | 429에만. 알고리즘이 계산한 ms 값 그대로 (Retry-After보다 정밀)                 |
| X-Decision-Us                  | 서버 내부 판정 지연(µs). 부하 발생기가 HTTP 왕복 지연과 분리해 기록한다        |
| X-Request-Id / X-Instance-Id   | 요청 ID(클라이언트가 보내면 그대로), 응답한 인스턴스                           |
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from rate_limit_lab.algorithms import ALGORITHMS, LeakingBucket, RateLimiter, create
from rate_limit_lab.clock import MonotonicClock
from rate_limit_lab.config import DEFAULT_RULES_PATH, load_rules
from rate_limit_lab.keys import RuleSelectionError, make_key, select_rules
from rate_limit_lab.metrics.memory import peak_rss_bytes, rss_bytes
from rate_limit_lab.metrics.summary import distribution
from rate_limit_lab.models import RecordError, Request, Rule, Status

CLOCK_SOURCE = "server_monotonic_ms (서버 시작 = 0)"



def limit_header(algorithm: str, rule: Rule) -> int:
    if algorithm == "token_bucket":
        return rule.bucket_capacity
    if algorithm == "leaking_bucket":
        return rule.queue_capacity
    return rule.limit


def retry_after_seconds(retry_after_ms: int) -> int:
    return max(1, math.ceil(retry_after_ms / 1000))


class RateLimitService:
    """알고리즘 인스턴스와 누출 버킷 처리 기록을 가진 서비스 상태."""

    def __init__(self, algorithm: str, rules: dict[str, Rule], *, instance_id: str = "local",
                 default_rule_id: str | None = None, work_ms: int = 0, evict_interval_ms: int = 1000,
                 clock_ms: Callable[[], int] | None = None) -> None:
        if algorithm not in ALGORITHMS:
            raise ValueError(f"알 수 없는 알고리즘 {algorithm!r}")
        self.algorithm = algorithm
        self.rules = rules
        self.instance_id = instance_id
        self.default_rule_id = default_rule_id or next(iter(rules))
        self.work_ms = work_ms
        self.evict_interval_ms = evict_interval_ms
        self.clock_ms = clock_ms or MonotonicClock().now_ms
        self.limiters: dict[str, RateLimiter] = {}
        self.outcomes: Counter[str] = Counter()
        self.decision_us: list[float] = []
        self.records: dict[str, dict[str, Any]] = {}  # 누출 버킷: request_id -> 접수·처리 기록
        self.events: list[dict[str, Any]] = []  # 누출 버킷 처리 완료 이벤트 (시간순)
        self.evicted = 0
        self._seq = 0
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # --- 판정 -----------------------------------------------------------------

    def limiter_for(self, rule: Rule) -> RateLimiter:
        limiter = self.limiters.get(rule.rule_id)
        if limiter is None:
            limiter = self.limiters[rule.rule_id] = create(self.algorithm, rule)
        return limiter

    def next_request_id(self) -> str:
        self._seq += 1
        return f"{self.instance_id}-{self._seq:08d}"

    def decide(self, request: Request) -> tuple[Rule, Any, float, int]:
        rules = select_rules(request, self.rules)
        if len(rules) != 1:
            raise NotImplementedError("복수 규칙 결합은 7단계에서 구현한다")
        rule = rules[0]
        limiter = self.limiter_for(rule)
        key = make_key(rule, request)
        now = self.clock_ms()
        started = time.perf_counter_ns()
        if isinstance(limiter, LeakingBucket):
            verdict = limiter.admit(key, now, request.request_id)
        else:
            verdict = limiter.decide(key, now)
        elapsed_us = (time.perf_counter_ns() - started) / 1000
        self.decision_us.append(elapsed_us)
        if verdict.status is Status.QUEUED:
            self.records[request.request_id] = {
                "request_id": request.request_id, "key": key, "status": "queued",
                "admitted_at_ms": now, "processed_at_ms": None, "observed_at_ms": None,
            }
            self._wake.set()
        return rule, verdict, elapsed_us, now

    # --- 누출 버킷 비동기 처리 ---------------------------------------------------

    def collect_processed(self) -> int:
        """처리 완료 시각이 지난 요청을 이벤트로 옮긴다."""
        now = self.clock_ms()
        count = 0
        for limiter in self.limiters.values():
            if isinstance(limiter, LeakingBucket):
                for e in limiter.drain(now):
                    record = self.records[e.item_id]
                    record.update(status="processed", processed_at_ms=e.processed_at_ms, observed_at_ms=now)
                    self.events.append({
                        "request_id": e.item_id, "key": e.key, "admitted_at_ms": e.admitted_at_ms,
                        "processed_at_ms": e.processed_at_ms, "observed_at_ms": now, "instance_id": self.instance_id,
                    })
                    count += 1
        return count

    def _next_due(self) -> int | None:
        dues = [l.next_due_ms() for l in self.limiters.values() if isinstance(l, LeakingBucket)]
        dues = [d for d in dues if d is not None]
        if any(isinstance(l, LeakingBucket) and l.pending_events() for l in self.limiters.values()):
            return self.clock_ms()
        return min(dues) if dues else None

    async def drain_loop(self) -> None:
        """다음 처리 예정 시각까지 기다렸다가 처리 완료 이벤트를 기록한다."""
        while True:
            due = self._next_due()
            self._wake.clear()
            if due is None:
                await self._wake.wait()
                continue
            delay = (due - self.clock_ms()) / 1000
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                continue
            self.collect_processed()
            await asyncio.sleep(0)

    async def evict_loop(self) -> None:
        while True:
            await asyncio.sleep(self.evict_interval_ms / 1000)
            now = self.clock_ms()
            for limiter in self.limiters.values():
                self.evicted += limiter.evict_idle(now)

    async def start(self, app: web.Application) -> None:
        self._tasks = [asyncio.create_task(self.drain_loop()), asyncio.create_task(self.evict_loop())]

    async def stop(self, app: web.Application) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def stats(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "algorithm": self.algorithm,
            "clock_source": CLOCK_SOURCE,
            "now_ms": self.clock_ms(),
            "outcomes": dict(self.outcomes),
            "decision_us": distribution(self.decision_us),
            "active_keys": sum(l.active_keys() for l in self.limiters.values()),
            "state_entries": sum(l.state_entries() for l in self.limiters.values()),
            "evicted": self.evicted,
            "queued_records": len(self.records),
            "processed_events": len(self.events),
            "rss_bytes": rss_bytes(),
            "rss_peak_bytes": peak_rss_bytes(),
        }


# ---------------------------------------------------------------------------
# HTTP 핸들러

SERVICE = web.AppKey("service", RateLimitService)


def _json(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> web.Response:
    return web.json_response(data, status=status, headers=headers)


async def handle_work(request: web.Request) -> web.Response:
    service: RateLimitService = request.app[SERVICE]
    query = request.query
    client_id = query.get("client_id", "")
    if not client_id:
        service.outcomes["bad_request"] += 1
        return _json({"error": "client_id가 필요합니다"}, 400)
    request_id = request.headers.get("X-Request-Id") or query.get("request_id") or service.next_request_id()
    try:
        req = Request(
            request_id=request_id,
            scheduled_at_ms=service.clock_ms(),
            client_id=client_id,
            ip=query.get("ip") or request.remote or "unknown",
            endpoint=request.path,
            method=request.method,
            rule_id=query.get("rule_id", service.default_rule_id),
            instance_id=service.instance_id,
        )
        rule, verdict, elapsed_us, now = service.decide(req)
    except (RecordError, RuleSelectionError) as exc:
        service.outcomes["bad_request"] += 1
        return _json({"error": str(exc)}, 400)

    headers = {
        "X-Request-Id": request_id,
        "X-Instance-Id": service.instance_id,
        "X-RateLimit-Algorithm": service.algorithm,
        "X-RateLimit-Limit": str(limit_header(service.algorithm, rule)),
        "X-Decision-Us": f"{elapsed_us:.2f}",
    }
    if verdict.remaining is not None:
        headers["X-RateLimit-Remaining"] = str(verdict.remaining)
    body = {"request_id": request_id, "instance_id": service.instance_id, "status": verdict.status.value,
            "reason": verdict.reason, "decided_at_ms": now}

    if verdict.status is Status.REJECTED:
        service.outcomes["rejected_429"] += 1
        headers["Retry-After"] = str(retry_after_seconds(verdict.retry_after_ms))
        headers["X-RateLimit-Retry-After-Ms"] = str(verdict.retry_after_ms)
        body["retry_after_ms"] = verdict.retry_after_ms
        return _json(body, 429, headers)
    if verdict.status is Status.QUEUED:
        service.outcomes["queued_202"] += 1
        body["status_url"] = f"/requests/{request_id}"
        return _json(body, 202, headers)

    service.outcomes["ok_200"] += 1
    if service.work_ms:
        await asyncio.sleep(service.work_ms / 1000)  # 고정된 가상 작업
    return _json(body, 200, headers)


async def handle_request_status(request: web.Request) -> web.Response:
    service: RateLimitService = request.app[SERVICE]
    service.collect_processed()
    record = service.records.get(request.match_info["request_id"])
    if record is None:
        return _json({"error": "큐에 접수된 요청이 아닙니다"}, 404)
    return _json(record)


async def handle_events(request: web.Request) -> web.Response:
    service: RateLimitService = request.app[SERVICE]
    service.collect_processed()
    try:
        after = int(request.query.get("after", "0"))
    except ValueError:
        return _json({"error": "after는 정수여야 합니다"}, 400)
    events = service.events[after:]
    return _json({"events": events, "next": after + len(events), "clock_source": CLOCK_SOURCE})


async def handle_stats(request: web.Request) -> web.Response:
    service: RateLimitService = request.app[SERVICE]
    service.collect_processed()
    return _json(service.stats())


async def handle_health(request: web.Request) -> web.Response:
    service: RateLimitService = request.app[SERVICE]
    return _json({"status": "ok", "instance_id": service.instance_id, "algorithm": service.algorithm})


def create_app(service: RateLimitService) -> web.Application:
    app = web.Application()
    app[SERVICE] = service
    app.router.add_get("/work", handle_work)
    app.router.add_get("/requests/{request_id}", handle_request_status)
    app.router.add_get("/events", handle_events)
    app.router.add_get("/stats", handle_stats)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(service.start)
    app.on_cleanup.append(service.stop)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rate_limit_lab.api.server", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--algorithm", choices=sorted(ALGORITHMS), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--instance-id", default="A")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    parser.add_argument("--rule-id", default=None, help="client가 rule_id를 보내지 않을 때 쓸 규칙")
    parser.add_argument("--work-ms", type=int, default=0, help="허용된 요청마다 수행할 가상 작업 시간")
    parser.add_argument("--evict-interval-ms", type=int, default=1000)
    args = parser.parse_args(argv)

    service = RateLimitService(args.algorithm, load_rules(args.rules), instance_id=args.instance_id,
                               default_rule_id=args.rule_id, work_ms=args.work_ms,
                               evict_interval_ms=args.evict_interval_ms)
    print(f"instance {args.instance_id}: {args.algorithm} on http://{args.host}:{args.port}", flush=True)
    web.run_app(create_app(service), host=args.host, port=args.port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
