"""Token bucket (토큰 버킷).

비유: 키마다 토큰 통이 하나 있다.
- 통에는 최대 C개(`bucket_capacity`)가 들어가고, 처음에는 가득 차 있다.
- 토큰은 초당 r개(`refill_per_sec`)씩 **연속적으로** 채워진다. (100ms 지나면 r=10일 때 1개)
- 요청 1건 = 토큰 1개. 토큰이 있으면 허용하고 1개를 뺀다. 없으면 거절하고 아무것도 빼지 않는다.

특징: 쉬고 있던 사용자는 한 번에 C건까지 몰아서(버스트) 보낼 수 있다.

기본 정책(C=10, r=10/s) 예:
  0ms에 15건 → 10건 허용, 5건 거절(retry_after 100ms)
  100ms에 1건 → 토큰 1개가 다시 찼으므로 허용
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rate_limit_lab.algorithms.base import RateLimiter, Verdict
from rate_limit_lab.models import Rule, Status

# 실수 계산 오차(예: 0.9999999999) 때문에 토큰 1개를 놓치지 않도록 두는 여유.
EPS = 1e-9


@dataclass(slots=True)
class _Bucket:
    tokens: float  # updated_ms 시점의 토큰 수
    updated_ms: int
    last_seen_ms: int


class TokenBucket(RateLimiter):
    name = "token_bucket"

    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.capacity = rule.bucket_capacity
        self.refill_per_sec = rule.refill_per_sec

    def _tokens_at(self, bucket: _Bucket, now_ms: int) -> float:
        refilled = (now_ms - bucket.updated_ms) * self.refill_per_sec / 1000
        return min(self.capacity, bucket.tokens + refilled)

    def _decide(self, key: str, now_ms: int) -> Verdict:
        bucket = self._states.get(key)
        if bucket is None:
            bucket = self._states[key] = _Bucket(tokens=self.capacity, updated_ms=now_ms, last_seen_ms=now_ms)

        # 1) 지난 판정 이후 흐른 시간만큼 토큰을 채운다(용량 C를 넘지 않음).
        bucket.tokens = self._tokens_at(bucket, now_ms)
        bucket.updated_ms = now_ms
        bucket.last_seen_ms = now_ms

        # 2) 토큰이 1개 이상이면 허용하고 1개 소비.
        if bucket.tokens >= 1 - EPS:
            bucket.tokens = max(0.0, bucket.tokens - 1)
            return Verdict(Status.ALLOWED, math.floor(bucket.tokens + EPS), None, "token_consumed")

        # 3) 거절: 토큰을 소비하지 않는다. 토큰 1개가 찰 때까지 남은 시간을 알려 준다.
        wait_ms = math.ceil((1 - bucket.tokens) * 1000 / self.refill_per_sec - EPS)
        return Verdict(Status.REJECTED, 0, wait_ms, "no_token")

    def _is_fresh(self, bucket: _Bucket, now_ms: int) -> bool:
        # 토큰이 다시 가득 찼으면 처음 본 키와 같다.
        return self._tokens_at(bucket, now_ms) >= self.capacity - EPS
