"""다섯 가지 rate limiting 알고리즘.

| 이름              | 클래스                 | 파일                |
| ----------------- | ---------------------- | ------------------- |
| token_bucket      | TokenBucket            | token_bucket.py     |
| leaking_bucket    | LeakingBucket          | leaking_bucket.py   |
| fixed_window      | FixedWindowCounter     | fixed_window.py     |
| sliding_log       | SlidingWindowLog       | sliding_log.py      |  (strict 모드)
| sliding_log_pdf   | SlidingWindowLogPdf    | sliding_log.py      |  (PDF 재현 모드)
| sliding_counter   | SlidingWindowCounter   | sliding_counter.py  |
"""

from rate_limit_lab.algorithms.base import RateLimiter, Verdict
from rate_limit_lab.algorithms.fixed_window import FixedWindowCounter
from rate_limit_lab.algorithms.leaking_bucket import LeakingBucket, ProcessedEvent
from rate_limit_lab.algorithms.sliding_counter import SlidingWindowCounter
from rate_limit_lab.algorithms.sliding_log import SlidingWindowLog, SlidingWindowLogPdf
from rate_limit_lab.algorithms.token_bucket import TokenBucket
from rate_limit_lab.models import Rule

ALGORITHMS: dict[str, type[RateLimiter]] = {
    "token_bucket": TokenBucket,
    "leaking_bucket": LeakingBucket,
    "fixed_window": FixedWindowCounter,
    "sliding_log": SlidingWindowLog,
    "sliding_log_pdf": SlidingWindowLogPdf,
    "sliding_counter": SlidingWindowCounter,
}


def create(name: str, rule: Rule) -> RateLimiter:
    try:
        cls = ALGORITHMS[name]
    except KeyError:
        raise ValueError(f"알 수 없는 알고리즘 {name!r}. 가능: {sorted(ALGORITHMS)}") from None
    return cls(rule)


__all__ = [
    "ALGORITHMS",
    "FixedWindowCounter",
    "LeakingBucket",
    "ProcessedEvent",
    "RateLimiter",
    "SlidingWindowCounter",
    "SlidingWindowLog",
    "SlidingWindowLogPdf",
    "TokenBucket",
    "Verdict",
    "create",
]
