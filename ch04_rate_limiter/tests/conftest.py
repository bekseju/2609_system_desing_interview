from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from rate_limit_lab.models import Request, Rule, Scope

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_python(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """프로젝트 루트에서 `python <args>`를 실행한다. 출력은 UTF-8로 고정한다."""
    full_env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}
    return subprocess.run(
        [sys.executable, *args], cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", env=full_env
    )


def make_request(request_id: str = "r000001", scheduled_at_ms: int = 0, **overrides) -> Request:
    values = dict(
        request_id=request_id,
        scheduled_at_ms=scheduled_at_ms,
        client_id="user-1",
        ip="10.0.0.1",
        endpoint="/work",
        method="GET",
        rule_id="user_default",
        instance_id="local",
    )
    values.update(overrides)
    return Request(**values)


def make_rule(rule_id: str = "user_default", scope: Scope | str = Scope.USER, **overrides) -> Rule:
    values = dict(
        rule_id=rule_id,
        scope=scope,
        limit=10,
        window_ms=1000,
        bucket_capacity=10,
        refill_per_sec=10,
        queue_capacity=10,
        leak_per_sec=10,
        idle_ttl_ms=2000,
    )
    values.update(overrides)
    return Rule(**values)


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT
