"""0.4 공통 자료형과 CSV 직렬화/역직렬화."""

import pytest

from rate_limit_lab.models import (
    DECISION_FIELDS,
    REQUEST_FIELDS,
    Decision,
    RecordError,
    Status,
    read_decisions,
    read_requests,
    write_decisions,
    write_requests,
)
from tests.conftest import make_request


def make_decision(**overrides) -> Decision:
    values = dict(
        request_id="r000001",
        arrival_at_ms=0,
        decision_at_ms=0,
        status="allowed",
        processed_at_ms=None,
        retry_after_ms=None,
        remaining=9,
        latency_us=1.25,
        reason="within_limit",
    )
    values.update(overrides)
    return Decision(**values)


def test_field_order_matches_spec():
    assert REQUEST_FIELDS == (
        "request_id", "scheduled_at_ms", "client_id", "ip", "endpoint", "method", "rule_id", "instance_id",
    )
    assert DECISION_FIELDS == (
        "request_id", "arrival_at_ms", "decision_at_ms", "status", "processed_at_ms",
        "retry_after_ms", "remaining", "latency_us", "reason",
    )


def test_status_values():
    assert {s.value for s in Status} == {"allowed", "rejected", "queued", "processed"}
    assert make_decision(status="rejected").status is Status.REJECTED


def test_request_roundtrip(tmp_path):
    requests = [
        make_request("r000001", 0),
        make_request("r000002", 990, client_id="user,with \"comma\"", endpoint="/work?a=1"),
        make_request("r000003", 1010, client_id="사용자-2"),
    ]
    path = tmp_path / "requests.csv"
    write_requests(path, requests)
    assert read_requests(path) == requests


def test_decision_roundtrip_with_nulls(tmp_path):
    decisions = [
        make_decision(),
        make_decision(request_id="r2", status="rejected", remaining=0, retry_after_ms=100, reason="limit_exceeded"),
        make_decision(request_id="r3", status="queued", remaining=None, reason="enqueued"),
        make_decision(request_id="r4", arrival_at_ms=5, decision_at_ms=5, status="processed", processed_at_ms=105,
                      remaining=None, latency_us=0.1, reason="drained"),
    ]
    path = tmp_path / "decisions.csv"
    write_decisions(path, decisions)
    text = path.read_text(encoding="utf-8").splitlines()
    assert text[1] == "r000001,0,0,allowed,,,9,1.25,within_limit"
    assert read_decisions(path) == decisions


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (dict(request_id=""), "request_id: 비어 있지 않은"),
        (dict(client_id="  "), "client_id: 비어 있지 않은"),
        (dict(scheduled_at_ms=-1), "scheduled_at_ms: 0 이상"),
        (dict(scheduled_at_ms=1.5), "scheduled_at_ms: 정수가 필요"),
        (dict(scheduled_at_ms=True), "scheduled_at_ms: 정수가 필요"),
    ],
)
def test_request_validation(overrides, message):
    with pytest.raises(RecordError, match=message):
        make_request(**overrides)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (dict(status="denied"), "status: allowed, rejected, queued, processed"),
        (dict(status="processed"), "processed_at_ms가 필요"),
        (dict(status="allowed", processed_at_ms=5), "processed_at_ms는 null"),
        (dict(status="processed", decision_at_ms=10, arrival_at_ms=10, processed_at_ms=9), "processed_at_ms\\(9\\)가"),
        (dict(arrival_at_ms=10, decision_at_ms=9), "decision_at_ms\\(9\\)가 arrival_at_ms\\(10\\)"),
        (dict(retry_after_ms=-5), "retry_after_ms: 0 이상"),
        (dict(remaining=-1), "remaining: 0 이상"),
        (dict(latency_us=-0.1), "latency_us: 0 이상"),
        (dict(latency_us=float("nan")), "latency_us: 유한한 값"),
        (dict(reason=""), "reason: 비어 있지 않은"),
    ],
)
def test_decision_validation(overrides, message):
    with pytest.raises(RecordError, match=message):
        make_decision(**overrides)


def _write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


REQ_HEADER = ",".join(REQUEST_FIELDS)
DEC_HEADER = ",".join(DECISION_FIELDS)


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([], "헤더가 없습니다"),
        (["request_id,scheduled_at_ms"], "헤더가 명세와 다릅니다"),
        ([REQ_HEADER, "r1,0,u,ip,/w,GET,rule"], ":2: 열 개수 7 != 8"),
        ([REQ_HEADER, "r1,,u,ip,/w,GET,rule,local"], ":2: scheduled_at_ms: 필수 값이 비어"),
        ([REQ_HEADER, "r1,0,,ip,/w,GET,rule,local"], ":2: client_id: 필수 값이 비어"),
        ([REQ_HEADER, "r1,1.5,u,ip,/w,GET,rule,local"], "scheduled_at_ms: 정수여야 합니다"),
        ([REQ_HEADER, "r1,1s,u,ip,/w,GET,rule,local"], "scheduled_at_ms: 정수여야 합니다"),
        ([REQ_HEADER, "r1,0,u,ip,/w,GET,rule,local", "r1,1,u,ip,/w,GET,rule,local"], ":3: 중복 request_id"),
    ],
)
def test_read_requests_errors(tmp_path, lines, message):
    path = _write(tmp_path / "requests.csv", lines)
    with pytest.raises(RecordError, match=message):
        read_requests(path)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("r1,0,0,allowed,,,9,1.0,", "reason: 필수 값이 비어"),
        ("r1,0,0,,,,9,1.0,ok", "status: 필수 값이 비어"),
        ("r1,0,0,allowed,,0.5,9,1.0,ok", "retry_after_ms: 정수여야"),
        ("r1,0,0,allowed,,,9,fast,ok", "latency_us: 숫자여야"),
        ("r1,0,0,processed,,,,1.0,ok", "processed_at_ms가 필요"),
    ],
)
def test_read_decisions_errors(tmp_path, row, message):
    path = _write(tmp_path / "decisions.csv", [DEC_HEADER, row])
    with pytest.raises(RecordError, match=message):
        read_decisions(path)
