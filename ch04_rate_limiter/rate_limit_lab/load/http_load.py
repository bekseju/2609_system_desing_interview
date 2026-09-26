"""요청 CSV를 벽시계 속도로 HTTP 서버에 보내는 부하 발생기 (TODO 4.3, 4.5).

    python -m rate_limit_lab.load.http_load results/scenarios/normal-seed42/requests.csv \
        --url http://127.0.0.1:8081 --concurrency 32 --warmup-s 2 --measure-s 10 --out <dir>

동작
- 측정 구간: CSV에서 scheduled_at_ms < measure_s×1000인 요청을, 시작 시각 + scheduled_at_ms에 보낸다.
- 예열 구간: 측정 전에 CSV 앞부분 warmup_s초 분량을 client_id 앞에 "warmup-"를 붙여 보낸다.
  (연결·서버 준비용이며, 측정 대상 키의 제한 상태를 건드리지 않는다. 결과에는 넣지 않는다.)
- 동시성: 동시에 응답을 기다리는 요청 수의 상한. 꽉 차면 다음 요청은 자리가 날 때까지 기다리고,
  그만큼 발송 지연이 커진다.
- 인스턴스가 여러 개면 재생 순서대로 번갈아 보낸다(라운드 로빈).

요청마다 따로 기록하는 세 가지 시간 (섞지 않는다)
- send_lag_ms       : 실제 전송 시각 - 예정 시각 (부하 발생기 쪽 지연)
- rtt_ms            : 전송 시작 → 응답 본문 수신 완료 (HTTP 왕복)
- server_decision_us: 응답 헤더 X-Decision-Us (서버 안에서 알고리즘 판정에 걸린 시간)

결과 분류 (서로 겹치지 않는다)
- ok_200, queued_202, rejected_429 : 서버가 판정한 결과
- timeout        : 제한 시간 안에 응답을 받지 못함
- transport_error: 연결 실패·연결 끊김 등 전송 오류
- http_error     : 그 밖의 HTTP 상태 코드 (400, 500 등)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import aiohttp

from rate_limit_lab.clock import replay_order
from rate_limit_lab.metrics.summary import distribution
from rate_limit_lab.models import Request, read_requests

OUTCOMES = ("ok_200", "queued_202", "rejected_429", "timeout", "transport_error", "http_error")
STATUS_OUTCOME = {200: "ok_200", 202: "queued_202", 429: "rejected_429"}


@dataclass
class HttpResult:
    request_id: str
    scheduled_at_ms: int
    client_id: str
    instance: str
    sent_at_ms: float | None = None  # 측정 시작 기준
    send_lag_ms: float | None = None
    rtt_ms: float | None = None
    http_status: int | None = None
    outcome: str = ""
    server_decision_us: float | None = None
    remaining: int | None = None
    retry_after_s: int | None = None
    retry_after_ms: int | None = None
    server_decided_at_ms: int | None = None
    processed_at_server_ms: int | None = None  # 누출 버킷: 서버 시계 기준 처리 완료
    queue_wait_ms: int | None = None  # 누출 버킷: 처리 완료 - 접수 (서버 시계)
    error: str = ""


HTTP_RESULT_FIELDS = tuple(f.name for f in fields(HttpResult))


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _float_or_none(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


async def _send(session: aiohttp.ClientSession, url: str, request: Request, result: HttpResult,
                origin: float, timeout_s: float, semaphore: asyncio.Semaphore) -> None:
    started = time.perf_counter()
    result.sent_at_ms = (started - origin) * 1000
    result.send_lag_ms = result.sent_at_ms - request.scheduled_at_ms
    try:
        async with session.get(
            f"{url}/work",
            params={"client_id": request.client_id, "ip": request.ip, "rule_id": request.rule_id},
            headers={"X-Request-Id": request.request_id},
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as response:
            payload = await response.read()
            result.rtt_ms = (time.perf_counter() - started) * 1000
            result.http_status = response.status
            result.outcome = STATUS_OUTCOME.get(response.status, "http_error")
            h = response.headers
            result.server_decision_us = _float_or_none(h.get("X-Decision-Us"))
            result.remaining = _int_or_none(h.get("X-RateLimit-Remaining"))
            result.retry_after_s = _int_or_none(h.get("Retry-After"))
            result.retry_after_ms = _int_or_none(h.get("X-RateLimit-Retry-After-Ms"))
            if response.status in STATUS_OUTCOME:
                try:
                    result.server_decided_at_ms = json.loads(payload).get("decided_at_ms")
                except ValueError:
                    pass
    except TimeoutError:
        result.outcome = "timeout"
        result.error = f"no response within {timeout_s}s"
    except (aiohttp.ClientError, OSError) as exc:
        result.outcome = "transport_error"
        result.error = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        semaphore.release()


async def _replay(session, urls, requests, *, concurrency, timeout_s, label) -> tuple[list[HttpResult], float]:
    """requests를 예정 시각에 맞춰 보낸다. (결과, 전체 경과 초)"""
    semaphore = asyncio.Semaphore(concurrency)
    results, tasks = [], []
    origin = time.perf_counter() + 0.05
    for i, request in enumerate(requests):
        url = urls[i % len(urls)]
        result = HttpResult(request.request_id, request.scheduled_at_ms, request.client_id,
                            instance=f"{label}{i % len(urls)}")
        results.append(result)
        delay = origin + request.scheduled_at_ms / 1000 - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        await semaphore.acquire()  # 동시성 상한: 자리가 날 때까지 기다린다 (발송 지연에 반영)
        tasks.append(asyncio.create_task(_send(session, url, request, result, origin, timeout_s, semaphore)))
    await asyncio.gather(*tasks)
    return results, time.perf_counter() - origin


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> dict[str, Any] | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            return await response.json()
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
        return None


async def collect_server_data(urls: list[str], wait_for_queue_s: float = 0.0) -> tuple[list[dict], list[dict]]:
    """서버별 /stats와 처리 완료 이벤트 전체를 가져온다."""
    if wait_for_queue_s:
        await asyncio.sleep(wait_for_queue_s)  # 큐에 남은 요청이 처리될 시간을 준다
    stats, events = [], []
    async with aiohttp.ClientSession() as session:
        for url in urls:
            s = await _fetch_json(session, f"{url}/stats")
            stats.append(s or {"url": url, "error": "stats unavailable"})
            after = 0
            while True:
                page = await _fetch_json(session, f"{url}/events?after={after}")
                if not page or not page["events"]:
                    break
                events.extend(page["events"])
                after = page["next"]
    return stats, events


def warmup_requests(requests: list[Request], warmup_s: float) -> list[Request]:
    limit = warmup_s * 1000
    return [
        Request(f"w{r.request_id}", r.scheduled_at_ms, f"warmup-{r.client_id}", r.ip, r.endpoint, r.method,
                r.rule_id, r.instance_id)
        for r in requests if r.scheduled_at_ms < limit
    ]


async def run_load(requests: list[Request], urls: list[str], *, concurrency: int, measure_s: float,
                   warmup_s: float = 0.0, timeout_s: float = 5.0, queue_wait_s: float = 1.5) -> dict[str, Any]:
    ordered = replay_order(requests)
    measured = [r for r in ordered if r.scheduled_at_ms < measure_s * 1000]
    warm = warmup_requests(ordered, warmup_s)
    connector = aiohttp.TCPConnector(limit=0, force_close=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        warm_results, _ = await _replay(session, urls, warm, concurrency=concurrency, timeout_s=timeout_s,
                                        label="i") if warm else ([], 0.0)
        results, elapsed_s = await _replay(session, urls, measured, concurrency=concurrency, timeout_s=timeout_s,
                                           label="i")
    stats, events = await collect_server_data(urls, queue_wait_s if any(r.outcome == "queued_202" for r in results)
                                              else 0.0)
    by_id = {r.request_id: r for r in results}
    for e in events:
        r = by_id.get(e["request_id"])
        if r is not None:
            r.processed_at_server_ms = e["processed_at_ms"]
            r.queue_wait_ms = e["processed_at_ms"] - e["admitted_at_ms"]
    return {
        "results": results,
        "elapsed_s": elapsed_s,
        "warmup": {"sent": len(warm_results), "outcomes": dict(Counter(r.outcome for r in warm_results))},
        "server_stats": stats,
    }


# ---------------------------------------------------------------------------
# 요약·저장


def summarize_http(results: list[HttpResult], elapsed_s: float, server_stats: list[dict]) -> dict[str, Any]:
    outcomes = Counter(r.outcome for r in results)
    outcome_counts = {name: outcomes.get(name, 0) for name in OUTCOMES}
    processed = sum(1 for r in results if r.processed_at_server_ms is not None)
    # 다른 백엔드와 같은 형식의 상태별 건수 (정합성 검사용)
    status_counts = {
        "allowed": outcome_counts["ok_200"],
        "rejected": outcome_counts["rejected_429"],
        "queued": outcome_counts["queued_202"] - processed,  # 접수됐지만 처리 완료 이벤트가 없는 것
        "processed": processed,
        "error": outcome_counts["transport_error"] + outcome_counts["http_error"],
        "timeout": outcome_counts["timeout"],
    }
    responded = sum(outcome_counts[n] for n in ("ok_200", "queued_202", "rejected_429", "http_error"))
    rss = [s.get("rss_bytes") for s in server_stats if s.get("rss_bytes") is not None]
    return {
        "sent": len(results),
        "input_requests": len(results),
        "outcomes": outcome_counts,
        "status_counts": status_counts,
        "accepted": outcome_counts["ok_200"] + outcome_counts["queued_202"],
        "rejected": outcome_counts["rejected_429"],
        "processed_events": processed,
        "elapsed_s": round(elapsed_s, 3),
        "throughput_rps": round(responded / elapsed_s, 1) if elapsed_s else None,
        "send_lag_ms": distribution(r.send_lag_ms for r in results if r.send_lag_ms is not None),
        "rtt_ms": distribution(r.rtt_ms for r in results if r.rtt_ms is not None),
        "server_decision_us": distribution(r.server_decision_us for r in results
                                           if r.server_decision_us is not None),
        "queue_wait_ms": distribution(r.queue_wait_ms for r in results if r.queue_wait_ms is not None),
        "server": {
            "instances": len(server_stats),
            "rss_bytes_max": max(rss) if rss else None,
            "rss_bytes_sum": sum(rss) if rss else None,
            "clock_source": next((s.get("clock_source") for s in server_stats if s.get("clock_source")), None),
        },
    }


def write_http_results(path: Path, results: list[HttpResult]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HTTP_RESULT_FIELDS, lineterminator="\n")
        writer.writeheader()
        for r in results:
            row = asdict(r)
            for k in ("sent_at_ms", "send_lag_ms", "rtt_ms"):
                if row[k] is not None:
                    row[k] = round(row[k], 3)
            writer.writerow({k: "" if v is None else v for k, v in row.items()})


HTTP_TIMESERIES_FIELDS = ("bin_ms", "start_ms", "end_ms", "sent", *OUTCOMES)


def http_timeseries(results: list[HttpResult], bins_ms=(100, 1000)) -> list[dict[str, int]]:
    """전송 시각(측정 시작 기준) 구간 [start, end)별 결과 건수."""
    sent = [r for r in results if r.sent_at_ms is not None]
    last = max((int(r.sent_at_ms) for r in sent), default=0)
    rows = []
    for bin_ms in bins_ms:
        counters = {name: Counter() for name in ("sent", *OUTCOMES)}
        for r in sent:
            b = int(r.sent_at_ms) // bin_ms
            counters["sent"][b] += 1
            counters[r.outcome][b] += 1
        for index in range(last // bin_ms + 1):
            rows.append({"bin_ms": bin_ms, "start_ms": index * bin_ms, "end_ms": (index + 1) * bin_ms,
                         **{name: c.get(index, 0) for name, c in counters.items()}})
    return rows


def write_rows(path: Path, fieldnames, rows) -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rate_limit_lab.load.http_load", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("requests", type=Path)
    parser.add_argument("--url", action="append", required=True, help="서버 주소, 여러 번 지정 가능")
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--warmup-s", type=float, default=10)
    parser.add_argument("--measure-s", type=float, default=60)
    parser.add_argument("--timeout-s", type=float, default=5)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    data = asyncio.run(run_load(read_requests(args.requests), args.url, concurrency=args.concurrency,
                                measure_s=args.measure_s, warmup_s=args.warmup_s, timeout_s=args.timeout_s))
    summary = summarize_http(data["results"], data["elapsed_s"], data["server_stats"])
    args.out.mkdir(parents=True, exist_ok=True)
    write_http_results(args.out / "requests.csv", data["results"])
    write_rows(args.out / "timeseries.csv", HTTP_TIMESERIES_FIELDS, http_timeseries(data["results"]))
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary["outcomes"]), f"rtt p95={summary['rtt_ms']['p95']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
