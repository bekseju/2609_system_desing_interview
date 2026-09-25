"""이동 창 계산 도우미."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable


def max_in_moving_window(times: Iterable[int], window_ms: int) -> int:
    """시각 목록에서, 길이 W인 어떤 반열린 구간 [s, s+W)에 들어가는 최대 건수."""
    ordered = sorted(times)
    best, left = 0, 0
    for right, t in enumerate(ordered):
        while ordered[left] <= t - window_ms:
            left += 1
        best = max(best, right - left + 1)
    return best


def max_per_key_in_moving_window(pairs: Iterable[tuple[str, int]], window_ms: int) -> tuple[int, str | None]:
    """(키, 시각) 목록에서 키별 이동 창 최대 건수 중 가장 큰 값과 그 키."""
    by_key: dict[str, list[int]] = defaultdict(list)
    for key, t in pairs:
        by_key[key].append(t)
    best, best_key = 0, None
    for key in sorted(by_key):
        value = max_in_moving_window(by_key[key], window_ms)
        if value > best:
            best, best_key = value, key
    return best, best_key
