"""저장된 요청 CSV를 여러 알고리즘에 재생하고 결과·정확도 비교를 저장한다.

    python -m rate_limit_lab.load.run_replay results/scenarios/boundary-seed42/requests.csv
    python -m rate_limit_lab.load.run_replay REQUESTS.csv --algorithms fixed_window sliding_counter --no-latency

결과(<out> 기본값: requests.csv 옆의 replay/):
    <out>/<algorithm>/decisions.csv   요청별 최종 판정
    <out>/<algorithm>/events.csv      시간순 arrival/decision/processed 이벤트
    <out>/<algorithm>/summary.json    상태별 건수·키 수·이동 창 최대 허용 수
    <out>/accuracy.json               strict sliding log 대비 false allow/false reject 요약
    <out>/accuracy.csv                요청별 비교표
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rate_limit_lab.algorithms import ALGORITHMS
from rate_limit_lab.clock import replay_order
from rate_limit_lab.config import DEFAULT_RULES_PATH, load_rules
from rate_limit_lab.load.replay import replay, write_result
from rate_limit_lab.metrics.accuracy import REFERENCE, compare, write_accuracy_csv
from rate_limit_lab.models import read_requests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rate_limit_lab.load.run_replay", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("requests", type=Path)
    parser.add_argument("--algorithms", nargs="+", choices=sorted(ALGORITHMS), default=list(ALGORITHMS))
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--no-latency", action="store_true", help="판정 지연을 0으로 기록 (출력 파일을 완전히 결정적으로)")
    args = parser.parse_args(argv)

    requests = replay_order(read_requests(args.requests))
    rules = load_rules(args.rules)
    out = args.out or args.requests.parent / "replay"
    names = list(dict.fromkeys([REFERENCE, *args.algorithms]))  # 기준은 항상 포함

    decisions = {}
    for name in names:
        result = replay(requests, rules, name, measure_latency=not args.no_latency)
        write_result(out / name, result)
        decisions[name] = result.decisions
        s = result.stats
        print(f"{name:16s} accepted={s['accepted']:6d} rejected={s['rejected']:6d} "
              f"max/key/{s['moving_window_ms']}ms={s['max_accepted_per_key_in_moving_window']}")

    summaries, classes = {}, {}
    for name in names:
        if name == REFERENCE:
            continue
        summaries[name], classes[name] = compare(decisions[REFERENCE], decisions[name], name)
        a = summaries[name]
        print(f"  vs {REFERENCE}: {name:16s} false_allow={a['false_allow']} ({a['false_allow_rate']}) "
              f"false_reject={a['false_reject']} ({a['false_reject_rate']}) [{a['interpretation']}]")
    (out / "accuracy.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_accuracy_csv(out / "accuracy.csv", requests, decisions, classes)
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
