"""기준 알고리즘(strict sliding log) 대비 요청별 판정 비교.

요청마다 기준과 비교 대상의 "받아들였는가(허용·접수·처리)"를 비교해 네 가지로 나눈다.

| 분류          | 기준(strict log) | 비교 대상 |
| ------------- | ---------------- | --------- |
| agree_accept  | 받아들임         | 받아들임  |
| agree_reject  | 거절             | 거절      |
| false_allow   | 거절             | 받아들임  |  ← 한도를 넘겨 허용
| false_reject  | 받아들임         | 거절      |  ← 한도 안인데 거절

주의:
- 각 알고리즘은 **자기 판정 이력**으로 상태가 이어진다. 한 번 판정이 갈리면 이후 요청의 상태도
  달라지므로, 이 수치는 "같은 입력에 대한 두 정책의 판정 차이"이지 독립 시행의 오류율이 아니다.
- 근사 오차로 해석할 수 있는 것은 sliding_counter뿐이다. 나머지는 **정책 의미가 다르므로**
  차이는 오류가 아니라 정책 차이다(`POLICY_SEMANTICS`, `interpretation` 참고).
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rate_limit_lab.models import Decision, Request, Status

REFERENCE = "sliding_log"

POLICY_SEMANTICS: dict[str, str] = {
    "sliding_log": "기준. 허용 시각만 기록, 모든 이동 창 W에서 허용 <= L",
    "sliding_counter": "근사. 앞 칸 요청이 고르게 퍼졌다고 가정, 추정치 < L이면 허용",
    "fixed_window": "고정 칸마다 L건. 칸 경계에서 이동 창 W 안에 최대 2L 허용",
    "sliding_log_pdf": "거절 시각도 기록. 과부하가 이어지면 기준보다 더 많이 거절",
    "token_bucket": "용량 C 버스트 + 초당 r 재충전. 장기 평균은 r이지만 이동 창 상한이 아님",
    "leaking_bucket": "큐 Q + 초당 r 처리. '허용'이 아니라 '접수 후 지연 처리', 출력 속도 고정",
}
INTERPRETATION: dict[str, str] = {
    "sliding_log": "reference",
    "sliding_counter": "approximation_error",
}
CLASSES = ("agree_accept", "agree_reject", "false_allow", "false_reject")


def _accepted(decision: Decision) -> bool:
    return decision.status is not Status.REJECTED


def classify(reference_accepted: bool, candidate_accepted: bool) -> str:
    if reference_accepted == candidate_accepted:
        return "agree_accept" if reference_accepted else "agree_reject"
    return "false_allow" if candidate_accepted else "false_reject"


def compare(reference: Sequence[Decision], candidate: Sequence[Decision], algorithm: str) -> tuple[dict[str, Any], list[str]]:
    """요청별 분류 목록과 요약을 돌려준다. 두 목록은 같은 요청을 같은 순서로 담아야 한다."""
    if [d.request_id for d in reference] != [d.request_id for d in candidate]:
        raise ValueError("기준과 비교 대상의 요청 목록(순서 포함)이 다릅니다")
    classes = [classify(_accepted(r), _accepted(c)) for r, c in zip(reference, candidate)]
    counts = Counter(classes)
    total = len(classes)
    ref_accept = counts["agree_accept"] + counts["false_reject"]
    ref_reject = counts["agree_reject"] + counts["false_allow"]

    def ratio(n: int, d: int) -> float | None:
        return round(n / d, 6) if d else None

    summary = {
        "algorithm": algorithm,
        "reference": REFERENCE,
        "interpretation": INTERPRETATION.get(algorithm, "policy_difference"),
        "policy_semantics": POLICY_SEMANTICS.get(algorithm, ""),
        "total": total,
        **{name: counts.get(name, 0) for name in CLASSES},
        "false_allow_rate": ratio(counts["false_allow"], total),
        "false_reject_rate": ratio(counts["false_reject"], total),
        "false_allow_rate_of_reference_rejected": ratio(counts["false_allow"], ref_reject),
        "false_reject_rate_of_reference_accepted": ratio(counts["false_reject"], ref_accept),
        "agreement_rate": ratio(counts["agree_accept"] + counts["agree_reject"], total),
    }
    return summary, classes


def write_accuracy_csv(path: Path, requests: Sequence[Request], results: Mapping[str, Sequence[Decision]],
                       classes: Mapping[str, list[str]]) -> None:
    """요청별 비교표: 요청 정보, 기준 상태, 알고리즘별 상태와 분류."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    names = [n for n in results if n != REFERENCE]
    reference = results[REFERENCE]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["request_id", "scheduled_at_ms", "client_id", f"{REFERENCE}_status"]
                        + [col for n in names for col in (f"{n}_status", f"{n}_class")])
        for i, request in enumerate(requests):
            row = [request.request_id, request.scheduled_at_ms, request.client_id, reference[i].status.value]
            for n in names:
                row += [results[n][i].status.value, classes[n][i]]
            writer.writerow(row)
