"""4.1 GET /work 200/429와 헤더, 4.2 누출 버킷 202·비동기 처리·조회."""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rate_limit_lab.algorithms import ALGORITHMS
from rate_limit_lab.api.server import RateLimitService, create_app, retry_after_seconds
from rate_limit_lab.config import load_rules

RULES = load_rules()


class FakeClock:
    def __init__(self, t: int = 0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def run_with_client(algorithm, scenario, clock=None, **kwargs):
    """서버를 프로세스 안에서 띄우고 scenario(client, service)를 실행한다."""

    async def main():
        service = RateLimitService(algorithm, RULES, instance_id="T", clock_ms=clock, **kwargs)
        async with TestClient(TestServer(create_app(service))) as client:
            return await scenario(client, service)

    return asyncio.run(main())


async def get_work(client, client_id="alice", **params):
    response = await client.get("/work", params={"client_id": client_id, **params})
    return response.status, response.headers, await response.json()


# --- 4.1 ----------------------------------------------------------------------


def test_token_bucket_200_then_429_with_headers():
    async def scenario(client, service):
        return [await get_work(client) for _ in range(15)]

    results = run_with_client("token_bucket", scenario, FakeClock(0))
    assert [s for s, _, _ in results] == [200] * 10 + [429] * 5
    assert [h["X-RateLimit-Remaining"] for _, h, _ in results[:10]] == [str(i) for i in range(9, -1, -1)]
    assert all(h["X-RateLimit-Limit"] == "10" for _, h, _ in results)
    status, headers, body = results[-1]
    assert headers["Retry-After"] == "1"  # 100ms → 1초로 올림 (HTTP 규격은 초 단위)
    assert headers["X-RateLimit-Retry-After-Ms"] == "100"
    assert body["retry_after_ms"] == 100 and body["status"] == "rejected"
    assert float(headers["X-Decision-Us"]) >= 0
    assert headers["X-Instance-Id"] == "T"


def test_retry_after_rounds_up_to_seconds():
    assert [retry_after_seconds(ms) for ms in (1, 999, 1000, 1001, 2500)] == [1, 1, 1, 2, 3]


def test_request_id_is_echoed_and_generated():
    async def scenario(client, service):
        r1 = await client.get("/work", params={"client_id": "a"}, headers={"X-Request-Id": "my-id"})
        r2 = await client.get("/work", params={"client_id": "a"})
        return r1.headers["X-Request-Id"], r2.headers["X-Request-Id"]

    given, generated = run_with_client("fixed_window", scenario, FakeClock(0))
    assert given == "my-id" and generated.startswith("T-")


def test_fixed_window_retry_after_at_boundary():
    clock = FakeClock(990)

    async def scenario(client, service):
        return [await get_work(client) for _ in range(11)]

    results = run_with_client("fixed_window", scenario, clock)
    assert results[-1][0] == 429
    assert results[-1][1]["X-RateLimit-Retry-After-Ms"] == "10"
    assert results[-1][1]["Retry-After"] == "1"


def test_clients_are_isolated_and_time_recovers():
    clock = FakeClock(0)

    async def scenario(client, service):
        alice = [(await get_work(client, "alice"))[0] for _ in range(12)]
        bob = (await get_work(client, "bob"))[0]
        clock.t = 1000
        alice_later = (await get_work(client, "alice"))[0]
        return alice, bob, alice_later

    alice, bob, alice_later = run_with_client("sliding_log", scenario, clock)
    assert alice.count(200) == 10 and alice.count(429) == 2
    assert bob == 200 and alice_later == 200


def test_bad_requests():
    async def scenario(client, service):
        missing = await client.get("/work")
        unknown_rule = await client.get("/work", params={"client_id": "a", "rule_id": "nope"})
        return missing.status, unknown_rule.status, (await unknown_rule.json())["error"], service.outcomes

    missing, unknown, error, outcomes = run_with_client("token_bucket", scenario, FakeClock(0))
    assert (missing, unknown) == (400, 400) and "rule_id" in error
    assert outcomes["bad_request"] == 2


@pytest.mark.parametrize("name", sorted(ALGORITHMS))
def test_every_algorithm_serves_and_limits(name):
    async def scenario(client, service):
        return [await get_work(client) for _ in range(12)]

    results = run_with_client(name, scenario, FakeClock(0))
    ok = 202 if name == "leaking_bucket" else 200
    assert [s for s, _, _ in results] == [ok] * 10 + [429] * 2
    assert results[0][1]["X-RateLimit-Limit"] == "10"
    assert int(results[-1][1]["Retry-After"]) >= 1


def test_stats_endpoint():
    async def scenario(client, service):
        for _ in range(12):
            await get_work(client)
        response = await client.get("/stats")
        health = await client.get("/health")
        return await response.json(), health.status

    stats, health = run_with_client("token_bucket", scenario, FakeClock(0))
    assert health == 200
    assert stats["outcomes"] == {"ok_200": 10, "rejected_429": 2}
    assert stats["decision_us"]["count"] == 12 and stats["active_keys"] == 1
    assert stats["rss_bytes"] > 0 and "monotonic" in stats["clock_source"]


# --- 4.2 누출 버킷 ----------------------------------------------------------------


def test_leaking_bucket_202_then_processed_events_fake_clock():
    clock = FakeClock(0)

    async def scenario(client, service):
        results = [await get_work(client) for _ in range(12)]
        first_id = results[0][2]["request_id"]
        before = await (await client.get(f"/requests/{first_id}")).json()
        clock.t = 250
        events = await (await client.get("/events")).json()
        after = await (await client.get(f"/requests/{first_id}")).json()
        more = await (await client.get("/events", params={"after": events["next"]})).json()
        missing = await client.get("/requests/unknown")
        return results, before, events, after, more, missing.status

    results, before, events, after, more, missing = run_with_client("leaking_bucket", scenario, clock)
    assert [s for s, _, _ in results] == [202] * 10 + [429] * 2
    assert results[0][2]["status_url"] == f"/requests/{results[0][2]['request_id']}"
    assert results[-1][1]["X-RateLimit-Retry-After-Ms"] == "100"  # 맨 앞 요청이 100ms에 처리되면 자리 생김
    assert before["status"] == "queued" and before["processed_at_ms"] is None
    assert [e["processed_at_ms"] for e in events["events"]] == [100, 200]
    assert events["next"] == 2
    assert after["status"] == "processed" and after["admitted_at_ms"] == 0 and after["processed_at_ms"] == 100
    assert more["events"] == []
    assert missing == 404


def test_leaking_bucket_worker_processes_in_real_time():
    async def scenario(client, service):
        statuses = [(await get_work(client))[0] for _ in range(3)]
        await asyncio.sleep(0.6)  # 작업자가 100ms 간격으로 처리
        events = (await (await client.get("/events")).json())["events"]
        return statuses, events, service

    statuses, events, service = run_with_client("leaking_bucket", scenario)
    assert statuses == [202, 202, 202]
    assert len(events) == 3
    processed = [e["processed_at_ms"] for e in events]
    assert all(b - a >= 100 for a, b in zip(processed, processed[1:]))
    # 작업자가 요청 없이도 스스로 처리 이벤트를 기록했다 (관찰 시각은 예정 시각 이후)
    assert all(e["observed_at_ms"] >= e["processed_at_ms"] for e in events)
