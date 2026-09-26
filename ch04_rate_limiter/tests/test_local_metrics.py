"""3.1~3.7 로컬 측정·결과 폴더·보고서."""

import csv
import json
from functools import cache

import pytest

from rate_limit_lab.config import load_rules
from rate_limit_lab.load.replay import replay
from rate_limit_lab.load.scenarios import generate, load_scenarios, write_generated
from rate_limit_lab.metrics.memory import peak_rss_bytes, rss_bytes, traced_peak
from rate_limit_lab.metrics.report import build_report
from rate_limit_lab.metrics.run_local import run_replicate
from rate_limit_lab.metrics.summary import (
    ConsistencyError,
    check_consistency,
    distribution,
    percentile,
    summarize_decisions,
)
from rate_limit_lab.metrics.timeseries import build_timeseries, moving_window_maxima
from rate_limit_lab.models import Decision, RESULT_FIELDS, Status, read_request_results
from tests.conftest import make_request, run_python

RULES = load_rules()


def decision(rid, t, status="allowed", processed=None, latency=1.0):
    return Decision(rid, t, t, status, processed, None, None, latency, "x")


# --- 3.1 건수·처리량 -------------------------------------------------------------


def test_status_counts_sum_to_input():
    ds = [decision("a", 0), decision("b", 0, "rejected"), decision("c", 0, "processed", 100)]
    s = summarize_decisions(ds, wall_time_s=0.5)
    assert s["status_counts"] == {"allowed": 1, "rejected": 1, "queued": 0, "processed": 1, "error": 0, "timeout": 0}
    assert (s["accepted"], s["rejected"], s["admitted_to_queue"]) == (2, 1, 1)
    assert s["throughput_rps"] == 6.0


def test_consistency_check_detects_mismatch():
    s = summarize_decisions([decision("a", 0)], wall_time_s=1)
    s["input_requests"] = 2
    with pytest.raises(ConsistencyError, match="상태 합계"):
        check_consistency(s)


@pytest.mark.parametrize("scenario", ["normal", "overload"])
def test_replay_summary_matches_input(scenario):
    requests = generate(load_scenarios()[scenario], 42).requests
    for algorithm in ("token_bucket", "leaking_bucket"):
        s = summarize_decisions(replay(requests, RULES, algorithm).decisions, wall_time_s=1.0)
        assert sum(s["status_counts"].values()) == len(requests)


# --- 3.2 구간별 건수 -------------------------------------------------------------


def test_timeseries_half_open_bins():
    requests = [make_request(f"r{i}", t) for i, t in enumerate([0, 99, 100, 999, 1000])]
    ds = [decision(r.request_id, r.scheduled_at_ms) for r in requests]
    rows = build_timeseries(requests, ds, duration_ms=2000, bins_ms=(100, 1000))
    by = {(r["bin_ms"], r["start_ms"]): r for r in rows}
    assert by[(100, 0)]["arrivals"] == 2  # 0, 99
    assert by[(100, 100)]["arrivals"] == 1  # 100은 다음 구간
    assert by[(100, 900)]["arrivals"] == 1  # 999
    assert by[(1000, 0)]["arrivals"] == 4  # 0, 99, 100, 999
    assert by[(1000, 1000)]["arrivals"] == 1  # 1000은 [1000, 2000)
    assert all(r["end_ms"] - r["start_ms"] == r["bin_ms"] for r in rows)
    for b in (100, 1000):
        assert sum(r["arrivals"] for r in rows if r["bin_ms"] == b) == 5


def test_timeseries_processed_and_served_for_queue():
    requests = [make_request("a", 0), make_request("b", 0), make_request("c", 50)]
    ds = [decision("a", 0, "processed", 100), decision("b", 0, "processed", 200), decision("c", 50, "rejected")]
    rows = [r for r in build_timeseries(requests, ds, duration_ms=300, bins_ms=(100,))]
    assert [(r["accepted"], r["rejected"], r["processed"], r["served"]) for r in rows] == [
        (2, 1, 0, 0), (0, 0, 1, 1), (0, 0, 1, 1)]


def test_moving_window_maxima_boundary():
    requests = generate(load_scenarios()["boundary"], 42).requests
    result = replay(requests, RULES, "fixed_window")
    m = moving_window_maxima(result.keys, result.decisions, 1000)
    assert m["max_accepted_per_key"] == m["max_accepted_all_keys"] == 20


# --- 3.3 백분위수 ---------------------------------------------------------------


def test_percentile_nearest_rank_and_empty():
    values = list(range(1, 101))
    assert (percentile(values, 50), percentile(values, 95), percentile(values, 99)) == (50, 95, 99)
    assert percentile([7], 99) == 7
    assert distribution([]) == {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}


def test_queue_wait_empty_for_non_queue_algorithm():
    s = summarize_decisions([decision("a", 0)], wall_time_s=1)
    assert s["queue_wait_ms"]["count"] == 0 and s["queue_wait_ms"]["p95"] is None
    q = summarize_decisions([decision("a", 0, "processed", 100)], wall_time_s=1)
    assert q["queue_wait_ms"]["p50"] == 100


# --- 3.4 메모리 -----------------------------------------------------------------


def test_memory_measures():
    assert rss_bytes() > 0
    peak = peak_rss_bytes()
    assert peak is None or peak >= rss_bytes() * 0.5
    data, traced = traced_peak(lambda: [bytes(1000) for _ in range(1000)])
    assert traced >= 1_000_000 and len(data) == 1000


# --- 3.5 결과 폴더 --------------------------------------------------------------


@cache
def _replicate(tmp_root):
    from pathlib import Path

    root = Path(tmp_root)
    write_generated(root / "inputs" / "boundary", generate(load_scenarios()["boundary"], 42))
    out = root / "boundary" / "fixed_window" / "memory" / "r1"
    return out, run_replicate(root / "inputs" / "boundary", "fixed_window", out, replicate=1)


def test_run_replicate_writes_four_files(tmp_path):
    out, summary = _replicate(str(tmp_path))
    assert sorted(p.name for p in out.iterdir()) == ["environment.json", "requests.csv", "summary.json",
                                                      "timeseries.csv"]
    results = read_request_results(out / "requests.csv")
    assert len(results) == 20 and all(r.decision.status is Status.ALLOWED for r in results)
    assert next(csv.reader((out / "requests.csv").open(encoding="utf-8"))) == list(RESULT_FIELDS)

    assert summary["replay_consistent"] is True
    assert summary["seed"] == 42 and summary["rules"]["user_default"]["limit"] == 10
    assert summary["moving_window"]["max_accepted_per_key"] == 20
    assert summary["memory"]["algorithm_state_peak_bytes"] > 0
    assert summary["memory"]["tracemalloc_peak_bytes"] > 0
    assert set(summary["state"]) >= {"active_keys_peak", "active_keys_end_before_evict",
                                     "active_keys_end_after_evict"}

    env = json.loads((out / "environment.json").read_text(encoding="utf-8"))
    assert env["python"]["version"] and env["os"]["system"] and env["machine"]["logical_cpus"]
    assert env["scenario"]["seed"] == 42 and env["command"]


# --- 3.6·3.7 보고서와 한 줄 실행 --------------------------------------------------


def test_run_experiment_end_to_end(tmp_path):
    result = run_python("scripts/run_experiment.py", "--scenarios", "boundary", "hot_key",
                        "--algorithms", "fixed_window", "sliding_log", "sliding_counter",
                        "--replicates", "2", "--out", str(tmp_path))
    assert result.returncode == 0, result.stderr
    [exp] = list(tmp_path.iterdir())
    experiment = json.loads((exp / "experiment.json").read_text(encoding="utf-8"))
    assert len(experiment["plan"]) == 2 * 3 * 2 and experiment["failures"] == 0
    for scenario, algorithm, backend, rep in experiment["plan"]:
        assert (exp / scenario / algorithm / backend / f"r{rep}" / "summary.json").exists()

    rows = list(csv.DictReader((exp / "comparison.csv").open(encoding="utf-8")))
    assert len(rows) == 12
    assert {r["scenario"] for r in rows} == {"boundary", "hot_key"}
    for png in ("timeseries_boundary_memory.png", "latency_p95_memory.png", "memory_memory.png"):
        assert (exp / png).stat().st_size > 1000
    report = (exp / "report.md").read_text(encoding="utf-8")
    assert "정합성 문제 0건" in report and "미실행(`not_run`) 0건" in report
    assert "approximation_error" in report and "측정 범위와 단위" in report


def test_report_marks_not_run(tmp_path):
    out, _ = _replicate(str(tmp_path))
    exp = out.parents[3]
    plan = [["boundary", "fixed_window", "memory", 1], ["boundary", "sliding_log", "memory", 1]]
    (exp / "experiment.json").write_text(json.dumps({"plan": plan}), encoding="utf-8")
    report = build_report(exp).read_text(encoding="utf-8")
    assert "`not_run`: boundary/sliding_log/memory/1" in report
    assert "기준(sliding_log) 결과가 없어" in report
