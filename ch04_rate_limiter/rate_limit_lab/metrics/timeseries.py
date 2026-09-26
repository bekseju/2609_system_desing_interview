"""구간별 건수와 이동 창 최대 허용량 (TODO 3.2).

구간은 반열린 [start_ms, end_ms)이다. 999ms 요청은 [0, 1000) 구간, 1000ms 요청은 [1000, 2000) 구간.

열 의미 (각 구간에서 해당 사건이 일어난 건수):
- arrivals : 도착 (scheduled_at_ms 기준)
- accepted : 판정 시각에 받아들임 (허용 또는 큐 접수)
- rejected : 판정 시각에 거절
- processed: 누출 버킷 큐에서 실제 처리 완료 (processed_at_ms 기준)
- served   : 실제로 작업이 수행된 건수. 즉시 처리 알고리즘은 허용 시각, 누출 버킷은 처리 완료 시각
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rate_limit_lab.metrics.windows import max_in_moving_window, max_per_key_in_moving_window
from rate_limit_lab.models import Decision, Request, Status

TIMESERIES_FIELDS = ("bin_ms", "start_ms", "end_ms", "arrivals", "accepted", "rejected", "processed", "served")
DEFAULT_BINS_MS = (100, 1000)


def served_time(decision: Decision) -> int | None:
    """작업이 실제로 수행된 시각. 거절이면 None."""
    if decision.status is Status.REJECTED:
        return None
    if decision.processed_at_ms is not None:
        return decision.processed_at_ms
    if decision.status is Status.QUEUED:
        return None  # 아직 처리되지 않음
    return decision.decision_at_ms


def build_timeseries(requests: Sequence[Request], decisions: Sequence[Decision], *,
                     duration_ms: int, bins_ms: Sequence[int] = DEFAULT_BINS_MS) -> list[dict[str, int]]:
    last_event = max([duration_ms - 1] + [d.processed_at_ms for d in decisions if d.processed_at_ms is not None])
    rows: list[dict[str, int]] = []
    for bin_ms in bins_ms:
        counters = {name: Counter() for name in TIMESERIES_FIELDS[3:]}
        for r in requests:
            counters["arrivals"][r.scheduled_at_ms // bin_ms] += 1
        for d in decisions:
            b = d.decision_at_ms // bin_ms
            counters["rejected" if d.status is Status.REJECTED else "accepted"][b] += 1
            if d.processed_at_ms is not None:
                counters["processed"][d.processed_at_ms // bin_ms] += 1
            served = served_time(d)
            if served is not None:
                counters["served"][served // bin_ms] += 1
        for index in range(last_event // bin_ms + 1):
            start = index * bin_ms
            rows.append({"bin_ms": bin_ms, "start_ms": start, "end_ms": start + bin_ms,
                         **{name: c.get(index, 0) for name, c in counters.items()}})
    return rows


def moving_window_maxima(keys: Sequence[str], decisions: Sequence[Decision], window_ms: int) -> dict[str, Any]:
    """이동 창 W 안의 최대 건수: 전체 합계 기준과 키 하나 기준."""
    accepted = [(k, d.decision_at_ms) for k, d in zip(keys, decisions) if d.status is not Status.REJECTED]
    served = [(k, t) for k, d in zip(keys, decisions) if (t := served_time(d)) is not None]
    per_key, per_key_name = max_per_key_in_moving_window(accepted, window_ms)
    served_per_key, _ = max_per_key_in_moving_window(served, window_ms)
    return {
        "window_ms": window_ms,
        "max_accepted_all_keys": max_in_moving_window((t for _, t in accepted), window_ms),
        "max_accepted_per_key": per_key,
        "max_accepted_key": per_key_name,
        "max_served_all_keys": max_in_moving_window((t for _, t in served), window_ms),
        "max_served_per_key": served_per_key,
    }


def write_timeseries(path: Path, rows: Sequence[dict[str, int]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TIMESERIES_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
