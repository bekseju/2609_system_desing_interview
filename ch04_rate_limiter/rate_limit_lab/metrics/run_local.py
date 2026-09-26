"""로컬(메모리) 재생 1회차를 측정해 결과 폴더에 저장한다 (TODO 3.1~3.5).

    python -m rate_limit_lab.metrics.run_local --scenario-dir <requests.csv와 scenario.json이 있는 폴더> \
        --algorithm token_bucket --out results/<ts>/<scenario>/token_bucket/memory/r1 --replicate 1

메모리 peak는 프로세스 단위라서, 회차마다 새 프로세스에서 실행해야 한다(run_experiment가 그렇게 한다).

한 회차 = 같은 입력을 두 번 재생:
  1회차(timing): tracemalloc 없이 판정 지연·처리량 측정 → 저장되는 판정 결과
  2회차(memory): tracemalloc을 켜고 메모리 peak만 측정 (지연 측정 안 함)
두 회차의 판정은 같아야 한다(`replay_consistent`).

저장 파일:
  requests.csv     요청 원본 + 판정 결과 (요청마다 한 줄)
  timeseries.csv   100ms·1초 구간별 도착/허용/거절/처리/수행 건수
  summary.json     건수·처리량·지연·이동 창 최대·상태 키 수·메모리
  environment.json 설정·seed·CPU·OS·Python·패키지 버전·실행 명령
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from rate_limit_lab.algorithms import ALGORITHMS, LeakingBucket, create
from rate_limit_lab.clock import replay_order
from rate_limit_lab.config import DEFAULT_RULES_PATH, load_rules
from rate_limit_lab.load.replay import replay
from rate_limit_lab.load.scenarios import sha256_of
from rate_limit_lab.metrics.environment import collect_environment
from rate_limit_lab.metrics.memory import peak_rss_bytes, rss_bytes, traced_peak
from rate_limit_lab.metrics.summary import summarize_decisions
from rate_limit_lab.metrics.timeseries import build_timeseries, moving_window_maxima, write_timeseries
from rate_limit_lab.models import read_requests, write_request_results

BACKEND = "memory"

UNITS = {
    "decision_latency_us": "µs, decide() 호출 전후 perf_counter_ns 차이 (1회차)",
    "queue_wait_ms": "ms, 가상 시계 기준 처리 완료 - 접수",
    "throughput_rps": "요청/초, 입력 건수 / 1회차 재생 루프 벽시계 시간",
    "timeseries": "건수, 반열린 구간 [start_ms, end_ms), 가상 시계 기준",
    "memory.algorithm_state_peak_bytes": "bytes, 3회차(기록 없이 판정만) Python 할당 peak ≈ 알고리즘 상태",
    "memory.tracemalloc_peak_bytes": "bytes, 2회차 재생 중 Python 할당 peak (알고리즘 상태 + 재생기 기록)",
    "memory.rss_*_bytes": "bytes, 프로세스 전체 상주 메모리 (인터프리터·입력 데이터 포함)",
}


def _state_only_pass(keys: list[str], times: list[int], rule, algorithm: str, evict_interval_ms: int):
    """기록 없이 판정만 반복한다. tracemalloc peak ≈ 알고리즘 상태(+ 잠깐 생기는 판정 객체)."""
    limiter = create(algorithm, rule)
    leaking = isinstance(limiter, LeakingBucket)
    next_evict = evict_interval_ms
    for key, now in zip(keys, times):
        while next_evict <= now:
            limiter.evict_idle(next_evict)
            next_evict += evict_interval_ms
        if leaking:
            limiter.drain(now)
            limiter.admit(key, now)
        else:
            limiter.decide(key, now)
    return limiter.active_keys()


def run_replicate(scenario_dir: Path, algorithm: str, out_dir: Path, *, replicate: int = 1,
                  rules_path: Path = DEFAULT_RULES_PATH, evict_interval_ms: int = 1000,
                  command: list[str] | None = None) -> dict[str, Any]:
    scenario_dir, out_dir = Path(scenario_dir), Path(out_dir)
    meta = json.loads((scenario_dir / "scenario.json").read_text(encoding="utf-8"))
    requests_csv = scenario_dir / "requests.csv"
    requests = replay_order(read_requests(requests_csv))
    rules = load_rules(rules_path)
    rss_baseline = rss_bytes()

    # 1회차: 지연·처리량
    started = time.perf_counter()
    timed = replay(requests, rules, algorithm, measure_latency=True, evict_interval_ms=evict_interval_ms)
    wall_time_s = time.perf_counter() - started
    rss_after_timing = rss_bytes()

    # 2회차: 메모리 (지연은 재지 않음)
    traced, traced_peak_bytes = traced_peak(
        lambda: replay(requests, rules, algorithm, measure_latency=False, evict_interval_ms=evict_interval_ms)
    )
    consistent = [replace(d, latency_us=0.0) for d in timed.decisions] == traced.decisions

    # 3회차: 알고리즘 상태만 (키 문자열·시각 목록은 측정 전에 이미 만들어 둔 것을 쓴다)
    used_rules = sorted({r.rule_id for r in requests})
    times = [r.scheduled_at_ms for r in requests]
    state_peak_bytes = None
    if len(used_rules) == 1:
        _, state_peak_bytes = traced_peak(
            lambda: _state_only_pass(timed.keys, times, rules[used_rules[0]], algorithm, evict_interval_ms)
        )

    decisions = timed.decisions
    window_ms = min(rules[rid].window_ms for rid in used_rules)
    summary: dict[str, Any] = {
        "scenario": meta["scenario"],
        "algorithm": algorithm,
        "backend": BACKEND,
        "replicate": replicate,
        "seed": meta["seed"],
        "input_csv": str(requests_csv),
        "input_sha256": sha256_of(requests_csv),
        "duration_ms": meta["duration_ms"],
        "rules": {rid: {**asdict(rules[rid]), "scope": rules[rid].scope.value} for rid in used_rules},
        **summarize_decisions(decisions, wall_time_s=round(wall_time_s, 6)),
        "moving_window": moving_window_maxima(timed.keys, decisions, window_ms),
        "state": {k: timed.stats[k] for k in (
            "active_keys_peak", "evicted_during_run", "active_keys_end_before_evict",
            "state_entries_end_before_evict", "evicted_at_end", "active_keys_end_after_evict",
            "state_entries_end_after_evict")},
        "evict_interval_ms": evict_interval_ms,
        "memory": {
            "algorithm_state_peak_bytes": state_peak_bytes,
            "tracemalloc_peak_bytes": traced_peak_bytes,
            "rss_baseline_bytes": rss_baseline,
            "rss_after_timing_bytes": rss_after_timing,
            "rss_peak_bytes": peak_rss_bytes(),
        },
        "replay_consistent": consistent,
        "units": UNITS,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    write_request_results(out_dir / "requests.csv", requests, decisions)
    write_timeseries(out_dir / "timeseries.csv",
                     build_timeseries(requests, decisions, duration_ms=meta["duration_ms"]))
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    environment = collect_environment(
        command,
        scenario={k: v for k, v in meta.items() if k != "rps_bins"},  # 구간별 RPS는 scenario.json에 있다
        rules_file=str(rules_path),
        rules=summary["rules"],
        algorithm=algorithm,
        backend=BACKEND,
        replicate=replicate,
    )
    (out_dir / "environment.json").write_text(
        json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rate_limit_lab.metrics.run_local", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario-dir", type=Path, required=True)
    parser.add_argument("--algorithm", choices=sorted(ALGORITHMS), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--replicate", type=int, default=1)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    parser.add_argument("--evict-interval-ms", type=int, default=1000)
    args = parser.parse_args(argv)
    summary = run_replicate(args.scenario_dir, args.algorithm, args.out, replicate=args.replicate,
                            rules_path=args.rules, evict_interval_ms=args.evict_interval_ms,
                            command=[sys.executable, "-m", "rate_limit_lab.metrics.run_local", *(argv or sys.argv[1:])])
    lat = summary["decision_latency_us"]
    print(f"{summary['scenario']}/{args.algorithm}/r{args.replicate}: accepted={summary['accepted']} "
          f"rejected={summary['rejected']} p95={lat['p95']:.2f}us "
          f"state_peak={(summary['memory']['algorithm_state_peak_bytes'] or 0) / 1e3:.1f}KB "
          f"consistent={summary['replay_consistent']}")
    return 0 if summary["replay_consistent"] else 1


if __name__ == "__main__":
    sys.exit(main())
