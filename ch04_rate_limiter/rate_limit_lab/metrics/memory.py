"""메모리 측정 (TODO 3.4).

두 지표는 **측정 범위가 달라 직접 비교하지 않는다.**
- tracemalloc peak: 측정 구간 동안 Python이 새로 할당한 객체 메모리의 최대치(bytes).
  알고리즘 상태 + 재생기 기록을 포함하며, 인터프리터·라이브러리 자체는 포함하지 않는다.
- RSS: 운영체제가 본 프로세스 전체의 상주 메모리(bytes). 인터프리터·라이브러리·입력 데이터 포함.
  peak는 프로세스 시작 이후 최대치이므로, 실험마다 새 프로세스에서 재야 의미가 있다.
"""

from __future__ import annotations

import sys
import tracemalloc
from collections.abc import Callable
from typing import TypeVar

import psutil

T = TypeVar("T")


def rss_bytes() -> int:
    return psutil.Process().memory_info().rss


def peak_rss_bytes() -> int | None:
    """프로세스 시작 이후 RSS 최대치. 플랫폼이 제공하지 않으면 None."""
    info = psutil.Process().memory_info()
    peak = getattr(info, "peak_wset", None)  # Windows
    if peak is not None:
        return int(peak)
    try:
        import resource
    except ImportError:
        return None
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(maxrss if sys.platform == "darwin" else maxrss * 1024)  # Linux는 KiB 단위


def traced_peak(fn: Callable[[], T]) -> tuple[T, int]:
    """fn 실행 중 tracemalloc 추적 peak(bytes)를 잰다."""
    already = tracemalloc.is_tracing()
    if not already:
        tracemalloc.start()
    tracemalloc.reset_peak()
    base, _ = tracemalloc.get_traced_memory()
    try:
        result = fn()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not already:
            tracemalloc.stop()
    return result, peak - base
