"""누출 버킷 지속 부하 측정 (TODO 1.9).

한 키에 일정한 속도로 요청을 보내고, 접수·거절 건수, 실제 처리 속도, 대기 시간을 출력한다.
가상 시계를 쓰므로 결과는 항상 같다.

    python scripts/leaking_bucket_sustained.py                 # 기본: 5/10/20/50 req/s, 60초
    python scripts/leaking_bucket_sustained.py --rates 20 --seconds 10
"""

from __future__ import annotations

import argparse
import statistics

from rate_limit_lab.algorithms import LeakingBucket
from rate_limit_lab.config import load_rules
from rate_limit_lab.models import Status


def percentile(values: list[int], p: float) -> int:
    """nearest-rank 백분위수."""
    ordered = sorted(values)
    rank = max(1, round(p / 100 * len(ordered) + 0.5 - 1e-9))
    return ordered[min(rank, len(ordered)) - 1]


def measure(rate: int, seconds: int) -> dict[str, object]:
    rule = load_rules()["user_default"]
    limiter = LeakingBucket(rule)
    gap = 1000 // rate
    times = list(range(0, seconds * 1000, gap))
    verdicts = [limiter.admit("k", t, str(i)) for i, t in enumerate(times)]
    events = limiter.flush()

    processed = [e.processed_at_ms for e in events]
    waits = [e.wait_ms for e in events]
    # 처리 속도: 시작 1초(빈 줄에서 채워지는 구간) 이후 도착 구간에서의 초당 처리 건수
    steady = [p for p in processed if 1000 <= p < seconds * 1000]
    return {
        "arrival_rps": rate,
        "sent": len(times),
        "queued": sum(v.status is Status.QUEUED for v in verdicts),
        "rejected": sum(v.status is Status.REJECTED for v in verdicts),
        "processed": len(events),
        "processed_rps_steady": round(len(steady) / (seconds - 1), 2),
        "max_processed_in_1s": max(sum(1 for p in processed if s <= p < s + 1000) for s in range(0, processed[-1] + 1)),
        "wait_p50_ms": percentile(waits, 50),
        "wait_p95_ms": percentile(waits, 95),
        "wait_p99_ms": percentile(waits, 99),
        "wait_max_ms": max(waits),
        "wait_mean_ms": round(statistics.fmean(waits), 1),
        "drain_interval_ms": limiter.drain_interval_ms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rates", type=int, nargs="+", default=[5, 10, 20, 50])
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()

    rows = [measure(rate, args.seconds) for rate in args.rates]
    columns = list(rows[0])
    print(" | ".join(columns))
    for row in rows:
        print(" | ".join(str(row[c]) for c in columns))


if __name__ == "__main__":
    main()
