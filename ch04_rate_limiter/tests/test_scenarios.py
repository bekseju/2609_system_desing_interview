"""2.1~2.6 시나리오 설정·seed 기반 생성·패턴별 검증."""

import json
import statistics
from collections import Counter
from functools import cache

import pytest

from rate_limit_lab.algorithms import ALGORITHMS
from rate_limit_lab.clock import replay_order
from rate_limit_lab.config import load_rules
from rate_limit_lab.load.replay import replay
from rate_limit_lab.load.scenarios import (
    ScenarioConfigError,
    build_metadata,
    generate,
    load_scenarios,
    parse_scenarios,
    sha256_of,
    write_generated,
)
from rate_limit_lab.metrics.windows import max_in_moving_window
from rate_limit_lab.models import read_requests
from tests.conftest import run_python

RULES = load_rules()


@cache
def gen(name: str, seed: int = 42):
    return generate(load_scenarios()[name], seed)


# --- 2.1 설정·생성기 ----------------------------------------------------------


def test_default_scenarios_defined():
    assert list(load_scenarios()) == ["normal", "overload", "boundary", "unique_keys", "hot_key"]


def _config(**component):
    comp = {"type": "burst", "client_id": "u", "at_ms": 0, "count": 1, **component}
    return {
        "version": 1,
        "defaults": {"seed": 1, "rule_id": "r", "endpoint": "/w", "method": "GET", "instance_id": "local"},
        "scenarios": {"s": {"duration_ms": 1000, "components": [comp]}},
    }


@pytest.mark.parametrize(
    ("component", "message"),
    [
        (dict(type="storm"), "type은"),
        (dict(count=0), "count는 1 이상"),
        (dict(count=1.5), "count의 타입"),
        (dict(at_ms=1000), "at_ms < duration_ms"),
        (dict(extra=1), "알 수 없는 필드"),
        (dict(type="poisson", client_prefix="u", users=1, rate_per_user=0, start_ms=0, end_ms=10,
              client_id=None, at_ms=None, count=None), "알 수 없는 필드"),
    ],
)
def test_scenario_config_errors(component, message):
    with pytest.raises(ScenarioConfigError, match=message):
        parse_scenarios(_config(**component))


def test_poisson_range_errors():
    cfg = _config()
    cfg["scenarios"]["s"]["components"] = [
        {"type": "poisson", "client_prefix": "u", "users": 1, "rate_per_user": 5, "start_ms": 500, "end_ms": 400}
    ]
    with pytest.raises(ScenarioConfigError, match="start_ms < end_ms"):
        parse_scenarios(cfg)


@pytest.mark.parametrize("name", ["normal", "boundary", "unique_keys"])
def test_same_seed_same_file(tmp_path, name):
    scenario = load_scenarios()[name]
    a = write_generated(tmp_path / "a", generate(scenario, 42))
    b = write_generated(tmp_path / "b", generate(scenario, 42))
    assert a["requests_csv_sha256"] == b["requests_csv_sha256"]
    assert (tmp_path / "a" / "scenario.json").read_bytes() == (tmp_path / "b" / "scenario.json").read_bytes()
    assert read_requests(tmp_path / "a" / "requests.csv") == gen(name).requests  # CSV 왕복 후에도 같다


def test_different_seed_different_file(tmp_path):
    scenario = load_scenarios()["normal"]
    a = write_generated(tmp_path / "a", generate(scenario, 42))
    b = write_generated(tmp_path / "b", generate(scenario, 43))
    assert a["requests_csv_sha256"] != b["requests_csv_sha256"]


def test_same_seed_same_file_across_processes(tmp_path):
    hashes = set()
    for hash_seed in ("0", "999"):
        out = tmp_path / hash_seed
        result = run_python("-m", "rate_limit_lab.load.generate", "normal", "--seed", "5", "--out", str(out),
                            env={"PYTHONHASHSEED": hash_seed})
        assert result.returncode == 0, result.stderr
        hashes.add(sha256_of(out / "normal-seed5" / "requests.csv"))
    assert len(hashes) == 1


def test_cli_unknown_scenario():
    result = run_python("-m", "rate_limit_lab.load.generate", "nope")
    assert result.returncode == 2 and "알 수 없는 시나리오" in result.stderr


def test_metadata_counts_and_rps(tmp_path):
    meta = write_generated(tmp_path, gen("overload"))
    saved = json.loads((tmp_path / "scenario.json").read_text(encoding="utf-8"))
    assert saved == meta
    assert meta["seed"] == 42 and meta["rule_id"] == "user_default"
    assert meta["total_requests"] == len(gen("overload").requests)
    assert sum(c["generated"] for c in meta["components"]) == meta["total_requests"]
    assert sum(b["actual_rps"] for b in meta["rps_bins"]) == pytest.approx(meta["total_requests"])
    assert len(meta["rps_bins"]) == 60
    # 목표 RPS: 평소 500, 20~25초 500 + 5000
    targets = {b["start_ms"]: b["target_rps"] for b in meta["rps_bins"]}
    assert targets[0] == 500 and targets[20000] == 5500 and targets[24000] == 5500 and targets[25000] == 500


def test_request_ids_are_zero_padded_in_replay_order():
    requests = gen("normal").requests
    assert requests == replay_order(requests)
    assert [r.request_id for r in requests] == [f"r{i:08d}" for i in range(1, len(requests) + 1)]


# --- 2.2 일반 패턴 -------------------------------------------------------------


def test_normal_pattern_distribution():
    requests = gen("normal").requests
    n = len(requests)
    # 기대 30,000건 (100명 × 5 req/s × 60초). Poisson 표준편차 √30000 ≈ 173 → ±3σ
    assert abs(n - 30_000) < 3 * 173

    per_user = Counter(r.client_id for r in requests)
    assert len(per_user) == 100
    # 사용자별 기대 300건, 표준편차 √300 ≈ 17.3 → 모두 ±5σ 안
    assert all(300 - 87 < c < 300 + 87 for c in per_user.values())

    # 사용자별 도착 간격 평균 ≈ 200ms (5 req/s)
    gaps = []
    for user in per_user:
        times = [r.scheduled_at_ms for r in requests if r.client_id == user]
        gaps += [b - a for a, b in zip(times, times[1:])]
    assert statistics.fmean(gaps) == pytest.approx(200, rel=0.03)

    # Poisson이면 초당 건수의 분산/평균 ≈ 1
    per_user_second = Counter((r.client_id, r.scheduled_at_ms // 1000) for r in requests)
    counts = [per_user_second.get((u, s), 0) for u in per_user for s in range(60)]
    assert statistics.fmean(counts) == pytest.approx(5, rel=0.03)
    assert statistics.pvariance(counts) / statistics.fmean(counts) == pytest.approx(1.0, abs=0.1)

    assert all(0 <= r.scheduled_at_ms < 60_000 for r in requests)


# --- 2.3 과부하 패턴 -----------------------------------------------------------


def test_overload_adds_10x_in_window():
    normal, overload = gen("normal").requests, gen("overload").requests

    def in_window(rs):
        return [r for r in rs if 20_000 <= r.scheduled_at_ms < 25_000]

    base, peak = len(in_window(normal)), len(in_window(overload))
    extra = peak - base
    # 추가분 기대 25,000건 (100명 × 50 req/s × 5초), 표준편차 √25000 ≈ 158
    assert abs(extra - 25_000) < 3 * 158
    assert extra / base == pytest.approx(10, rel=0.1)

    # 기본 트래픽은 normal과 완전히 같다 (추가만 됐다)
    def signature(rs):
        return Counter((r.scheduled_at_ms, r.client_id) for r in rs)

    outside = [r for r in overload if not 20_000 <= r.scheduled_at_ms < 25_000]
    assert signature(outside) == signature([r for r in normal if not 20_000 <= r.scheduled_at_ms < 25_000])
    assert not (signature(normal) - signature(overload))  # normal의 모든 요청이 overload에 있다
    assert set(r.client_id for r in overload) == set(r.client_id for r in normal)


# --- 2.4 창 경계 패턴 ----------------------------------------------------------


def test_boundary_pattern_and_fixed_window_overshoot():
    requests = gen("boundary").requests
    assert Counter(r.scheduled_at_ms for r in requests) == {990: 10, 1010: 10}
    assert {r.client_id for r in requests} == {"user-boundary"}

    fixed = replay(requests, RULES, "fixed_window")
    strict = replay(requests, RULES, "sliding_log")
    fixed_times = [d.decision_at_ms for d in fixed.decisions if d.status.value != "rejected"]
    assert max_in_moving_window(fixed_times, 1000) == 20  # 이동 1초에 20건 > 한도 10
    assert fixed.stats["max_accepted_per_key_in_moving_window"] == 20
    assert strict.stats["max_accepted_per_key_in_moving_window"] == 10


# --- 2.5 고유 키 증가 패턴 -----------------------------------------------------


def test_unique_keys_pattern():
    requests = gen("unique_keys").requests
    assert len(requests) == 10_000
    assert len({r.client_id for r in requests}) == 10_000
    assert len({r.ip for r in requests}) == 10_000
    assert all(30_000 <= r.scheduled_at_ms < 40_000 for r in requests)


@pytest.mark.parametrize("name", sorted(ALGORITHMS))
def test_unique_keys_state_growth_and_eviction(name):
    requests = gen("unique_keys").requests
    no_evict = replay(requests, RULES, name, measure_latency=False, evict_interval_ms=None)
    with_evict = replay(requests, RULES, name, measure_latency=False, evict_interval_ms=1000)
    assert no_evict.stats["active_keys_peak"] == 10_000  # 제거하지 않으면 키가 계속 쌓인다
    assert with_evict.stats["active_keys_peak"] < 4_000  # 1초마다 제거: 최근 ~2~3초 분량만 남는다
    s = with_evict.stats
    assert s["evicted_during_run"] + s["evicted_at_end"] + s["active_keys_end_after_evict"] == 10_000
    assert [d.status for d in no_evict.decisions] == [d.status for d in with_evict.decisions]


# --- 2.6 핫 키 패턴 ------------------------------------------------------------


def test_hot_key_pattern():
    requests = gen("hot_key").requests
    assert len(requests) == 100
    assert {r.scheduled_at_ms for r in requests} == {0}
    assert {r.client_id for r in requests} == {"user-hot"}
    ids = [r.request_id for r in requests]
    assert ids == [f"r{i:08d}" for i in range(1, 101)] and len(set(ids)) == 100
    assert build_metadata(gen("hot_key"))["max_requests_same_ms"] == 100


@pytest.mark.parametrize("name", sorted(ALGORITHMS))
def test_hot_key_same_time_order_is_request_id(name):
    # 동시 100건 중 request_id 순으로 앞의 10건만 받아들여진다
    result = replay(list(reversed(gen("hot_key").requests)), RULES, name)
    accepted = [d.request_id for d in result.decisions if d.status.value != "rejected"]
    assert accepted == [f"r{i:08d}" for i in range(1, 11)]
