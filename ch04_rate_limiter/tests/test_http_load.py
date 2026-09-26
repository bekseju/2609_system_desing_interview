"""4.3 벽시계 부하 발생기, 4.4 실험 실행기, 4.5 타임아웃·전송 오류·429 분리."""

import asyncio
import csv
import json

from aiohttp.test_utils import TestServer

from rate_limit_lab.api.server import RateLimitService, create_app
from rate_limit_lab.config import load_rules
from rate_limit_lab.load.http_load import (
    HTTP_RESULT_FIELDS,
    OUTCOMES,
    http_timeseries,
    run_load,
    summarize_http,
    warmup_requests,
)
from rate_limit_lab.load.scenarios import generate, load_scenarios
from rate_limit_lab.metrics.summary import check_consistency
from scripts.run_http_experiment import free_port
from tests.conftest import make_request, run_python

RULES = load_rules()


def load_against(algorithm, requests, *, concurrency=8, measure_s=5.0, timeout_s=5.0, work_ms=0, warmup_s=0.0,
                 extra_urls=()):
    async def main():
        service = RateLimitService(algorithm, RULES, instance_id="T", work_ms=work_ms)
        server = TestServer(create_app(service))
        await server.start_server()
        try:
            url = str(server.make_url("")).rstrip("/")
            return await run_load(requests, [url, *extra_urls], concurrency=concurrency, measure_s=measure_s,
                                  warmup_s=warmup_s, timeout_s=timeout_s, queue_wait_s=1.2)
        finally:
            await server.close()

    return asyncio.run(main())


def hot_key(n=100):
    return generate(load_scenarios()["hot_key"], 42).requests[:n]


def test_outcomes_and_separate_latencies():
    data = load_against("token_bucket", hot_key())
    results = data["results"]
    summary = summarize_http(results, data["elapsed_s"], data["server_stats"])
    assert summary["outcomes"]["ok_200"] == 10 and summary["outcomes"]["rejected_429"] == 90
    assert sum(summary["outcomes"].values()) == summary["sent"] == 100
    check_consistency(summary)
    # 세 가지 시간이 요청마다 따로 기록된다
    assert all(r.rtt_ms is not None and r.rtt_ms > 0 for r in results)
    assert all(r.server_decision_us is not None for r in results)
    assert all(r.send_lag_ms is not None and r.send_lag_ms > -1 for r in results)
    # 서버 판정 지연(µs)은 왕복 지연(ms)보다 훨씬 작다 — 섞으면 안 되는 이유
    assert summary["server_decision_us"]["p50"] / 1000 < summary["rtt_ms"]["p50"]
    assert summary["server"]["rss_bytes_max"] > 0
    rejected = [r for r in results if r.outcome == "rejected_429"]
    assert all(r.retry_after_s >= 1 and r.retry_after_ms is not None for r in rejected)


def test_wall_clock_schedule_is_followed():
    # 0, 300, 600ms 예정 → 실제 전송 간격도 약 300ms
    requests = [make_request(f"r{i}", t, client_id=f"c{i}") for i, t in enumerate([0, 300, 600])]
    results = load_against("fixed_window", requests)["results"]
    sent = [r.sent_at_ms for r in results]
    assert 250 < sent[1] - sent[0] < 400 and 250 < sent[2] - sent[1] < 400
    assert all(r.send_lag_ms < 100 for r in results)


def test_concurrency_limit_creates_send_lag():
    # 서버 작업 100ms, 동시성 1 → 동시에 예정된 5건이 줄을 서서 발송 지연이 쌓인다
    requests = [make_request(f"r{i}", 0, client_id=f"c{i}") for i in range(5)]
    results = load_against("token_bucket", requests, concurrency=1, work_ms=100)["results"]
    lags = sorted(r.send_lag_ms for r in results)
    assert lags[-1] > 350  # 마지막 요청은 앞의 4건(각 100ms 이상)을 기다렸다
    assert all(r.outcome == "ok_200" for r in results)


def test_timeouts_are_separate_from_429():
    # 허용된 요청은 서버에서 1초 작업 → 0.3초 제한으로 타임아웃. 거절은 즉시 429.
    data = load_against("token_bucket", hot_key(20), concurrency=32, work_ms=1000, timeout_s=0.3)
    summary = summarize_http(data["results"], data["elapsed_s"], data["server_stats"])
    assert summary["outcomes"]["timeout"] == 10
    assert summary["outcomes"]["rejected_429"] == 10
    assert summary["status_counts"]["timeout"] == 10 and summary["status_counts"]["allowed"] == 0
    check_consistency(summary)


def test_transport_errors_are_separate():
    # 두 번째 인스턴스 주소에는 아무 서버도 없다 → 절반이 전송 오류
    dead = f"http://127.0.0.1:{free_port()}"
    requests = [make_request(f"r{i}", 0, client_id=f"c{i}") for i in range(10)]
    data = load_against("token_bucket", requests, extra_urls=[dead])
    summary = summarize_http(data["results"], data["elapsed_s"], data["server_stats"])
    assert summary["outcomes"]["transport_error"] == 5 and summary["outcomes"]["ok_200"] == 5
    assert summary["status_counts"]["error"] == 5
    assert all(r.error for r in data["results"] if r.outcome == "transport_error")
    check_consistency(summary)


def test_leaking_bucket_processing_is_joined():
    data = load_against("leaking_bucket", hot_key(15))
    summary = summarize_http(data["results"], data["elapsed_s"], data["server_stats"])
    assert summary["outcomes"]["queued_202"] == 10 and summary["outcomes"]["rejected_429"] == 5
    assert summary["processed_events"] == 10 and summary["status_counts"]["processed"] == 10
    waits = sorted(r.queue_wait_ms for r in data["results"] if r.queue_wait_ms is not None)
    # k번째 요청의 대기 ≈ 100×k ms (서버 시계). 요청이 몇 ms 차이로 도착하므로 그만큼 짧을 수 있다
    for k, wait in enumerate(waits, start=1):
        assert 100 * k - 50 <= wait <= 100 * k, waits


def test_warmup_uses_separate_keys():
    requests = generate(load_scenarios()["normal"], 42).requests
    warm = warmup_requests(requests, 0.5)
    assert warm and all(r.client_id.startswith("warmup-") and r.request_id.startswith("w") for r in warm)
    assert all(r.scheduled_at_ms < 500 for r in warm)


def test_http_timeseries_counts():
    data = load_against("token_bucket", hot_key(30))
    rows = http_timeseries(data["results"])
    for bin_ms in (100, 1000):
        part = [r for r in rows if r["bin_ms"] == bin_ms]
        assert sum(r["sent"] for r in part) == 30
        assert sum(sum(r[o] for o in OUTCOMES) for r in part) == 30


def test_run_http_experiment_reduced(tmp_path):
    result = run_python("scripts/run_http_experiment.py", "--algorithms", "token_bucket", "leaking_bucket",
                        "--instances", "1", "2", "--concurrency", "4", "--replicates", "1",
                        "--warmup-s", "0.5", "--measure-s", "1.5", "--out", str(tmp_path))
    assert result.returncode == 0, result.stderr + result.stdout
    [exp] = list(tmp_path.iterdir())
    experiment = json.loads((exp / "experiment.json").read_text(encoding="utf-8"))
    assert len(experiment["plan"]) == 4
    for scenario, algorithm, backend, rep in experiment["plan"]:
        out = exp / scenario / algorithm / backend / f"r{rep}"
        names = {p.name for p in out.iterdir()}
        assert {"requests.csv", "timeseries.csv", "summary.json", "environment.json"} <= names
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        check_consistency(summary)
        assert summary["instances"] == int(backend.split("-")[1][1:])
        assert sum(summary["outcomes"].values()) == summary["sent"] > 0
        header = next(csv.reader((out / "requests.csv").open(encoding="utf-8")))
        assert tuple(header) == HTTP_RESULT_FIELDS
        instances = {row["instance"] for row in csv.DictReader((out / "requests.csv").open(encoding="utf-8"))}
        assert len(instances) == summary["instances"]
    report = (exp / "report.md").read_text(encoding="utf-8")
    assert "## HTTP 부하 결과" in report and "정합성 문제 0건" in report
