"""판정 결과 집계: 상태별 건수, 처리량, 지연 백분위수 (TODO 3.1, 3.3).

단위: 판정 지연은 µs(마이크로초), 큐 대기는 ms, 처리량은 요청/초.
측정할 값이 하나도 없으면(예: 누출 버킷이 아닌 알고리즘의 큐 대기) 백분위수는 모두 None(빈 값)이다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

from rate_limit_lab.models import Decision, Status

# 로컬 재생에는 없지만 HTTP 실험과 같은 형식으로 비교하려고 항상 두는 상태
EXTRA_OUTCOMES = ("error", "timeout")


class ConsistencyError(AssertionError):
    """상태별 합계가 입력 건수와 맞지 않을 때 발생한다."""


def percentile(values: Sequence[float], p: float) -> float | None:
    """nearest-rank 백분위수: 정렬했을 때 ceil(p/100 × n)번째 값. 값이 없으면 None."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    data = list(values)
    return {
        "count": len(data),
        "p50": percentile(data, 50),
        "p95": percentile(data, 95),
        "p99": percentile(data, 99),
        "max": max(data) if data else None,
    }


def status_counts(decisions: Iterable[Decision]) -> dict[str, int]:
    counts = {s.value: 0 for s in Status} | {name: 0 for name in EXTRA_OUTCOMES}
    for d in decisions:
        counts[d.status.value] += 1
    return counts


def summarize_decisions(decisions: Sequence[Decision], *, wall_time_s: float | None) -> dict[str, Any]:
    """판정 목록을 요약한다.

    - accepted: 받아들인 요청(allowed + 큐 접수 후 queued/processed)
    - admitted_to_queue: 누출 버킷 큐에 접수된 요청(최종 queued + processed)
    - throughput_rps: 재생 루프의 실제 처리량 = 입력 건수 / 벽시계 경과 초
    """
    counts = status_counts(decisions)
    accepted = counts["allowed"] + counts["queued"] + counts["processed"]
    waits = [d.processed_at_ms - d.decision_at_ms for d in decisions if d.processed_at_ms is not None]
    summary = {
        "input_requests": len(decisions),
        "status_counts": counts,
        "accepted": accepted,
        "admitted_to_queue": counts["queued"] + counts["processed"],
        "rejected": counts["rejected"],
        "wall_time_s": wall_time_s,
        "throughput_rps": round(len(decisions) / wall_time_s, 1) if wall_time_s else None,
        "decision_latency_us": distribution(d.latency_us for d in decisions),
        "queue_wait_ms": distribution(waits),
    }
    check_consistency(summary)
    return summary


def check_consistency(summary: dict[str, Any]) -> None:
    """상태별 건수 합계 = 입력 건수, accepted + rejected + error + timeout = 입력 건수."""
    counts, total = summary["status_counts"], summary["input_requests"]
    if sum(counts.values()) != total:
        raise ConsistencyError(f"상태 합계 {sum(counts.values())} != 입력 {total}")
    if summary["accepted"] + summary["rejected"] + sum(counts[n] for n in EXTRA_OUTCOMES) != total:
        raise ConsistencyError("accepted + rejected + error + timeout != 입력 건수")
