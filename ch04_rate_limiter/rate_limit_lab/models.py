"""공통 자료형(`Request`, `Decision`, `Rule`)과 CSV 직렬화.

단위 규칙: 이름이 `_ms`로 끝나는 필드는 정수 ms, `latency_us`는 마이크로초(실수),
`*_per_sec`는 건/초. CSV에서 null은 빈 문자열로 표현한다.
"""

from __future__ import annotations

import csv
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class RecordError(ValueError):
    """레코드 또는 CSV 내용이 명세를 만족하지 않을 때 발생한다."""


class Status(StrEnum):
    ALLOWED = "allowed"
    REJECTED = "rejected"
    QUEUED = "queued"
    PROCESSED = "processed"


class Scope(StrEnum):
    GLOBAL = "global"
    USER = "user"
    IP = "ip"
    ENDPOINT = "endpoint"


# ---------------------------------------------------------------------------
# 값 검사

_INT_RE = re.compile(r"[+-]?\d+")


def _check_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecordError(f"{name}: 비어 있지 않은 문자열이 필요합니다 (값: {value!r})")
    return value


def _check_int(name: str, value: object, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecordError(f"{name}: 정수가 필요합니다 (값: {value!r})")
    if value < minimum:
        raise RecordError(f"{name}: {minimum} 이상이어야 합니다 (값: {value})")
    return value


def _check_optional_int(name: str, value: object) -> int | None:
    return None if value is None else _check_int(name, value)


def _check_number(name: str, value: object, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecordError(f"{name}: 숫자가 필요합니다 (값: {value!r})")
    value = float(value)
    if not math.isfinite(value):
        raise RecordError(f"{name}: 유한한 값이어야 합니다 (값: {value})")
    if value < 0 or (positive and value == 0):
        raise RecordError(f"{name}: {'0보다 커야' if positive else '0 이상이어야'} 합니다 (값: {value})")
    return value


# ---------------------------------------------------------------------------
# 자료형


@dataclass(frozen=True, slots=True)
class Request:
    """입력 요청 레코드. `scheduled_at_ms`는 시뮬레이션 시작(0ms) 이후 정수 ms."""

    request_id: str
    scheduled_at_ms: int
    client_id: str
    ip: str
    endpoint: str
    method: str
    rule_id: str
    instance_id: str

    def __post_init__(self) -> None:
        for name in ("request_id", "client_id", "ip", "endpoint", "method", "rule_id", "instance_id"):
            _check_text(name, getattr(self, name))
        _check_int("scheduled_at_ms", self.scheduled_at_ms)


@dataclass(frozen=True, slots=True)
class Decision:
    """요청 하나의 판정 결과. `request_id`로 원본 `Request`와 연결된다.

    - `processed` 상태만 `processed_at_ms`를 가지며, 그 외 상태는 null이다.
    - 시각 순서: arrival_at_ms <= decision_at_ms <= processed_at_ms.
    """

    request_id: str
    arrival_at_ms: int
    decision_at_ms: int
    status: Status
    processed_at_ms: int | None
    retry_after_ms: int | None
    remaining: int | None
    latency_us: float
    reason: str

    def __post_init__(self) -> None:
        _check_text("request_id", self.request_id)
        _check_int("arrival_at_ms", self.arrival_at_ms)
        _check_int("decision_at_ms", self.decision_at_ms)
        try:
            object.__setattr__(self, "status", Status(self.status))
        except ValueError:
            allowed = ", ".join(s.value for s in Status)
            raise RecordError(f"status: {allowed} 중 하나여야 합니다 (값: {self.status!r})") from None
        _check_optional_int("processed_at_ms", self.processed_at_ms)
        _check_optional_int("retry_after_ms", self.retry_after_ms)
        _check_optional_int("remaining", self.remaining)
        object.__setattr__(self, "latency_us", _check_number("latency_us", self.latency_us))
        _check_text("reason", self.reason)

        if self.decision_at_ms < self.arrival_at_ms:
            raise RecordError(
                f"decision_at_ms({self.decision_at_ms})가 arrival_at_ms({self.arrival_at_ms})보다 앞섭니다"
            )
        if self.status is Status.PROCESSED:
            if self.processed_at_ms is None:
                raise RecordError("status=processed이면 processed_at_ms가 필요합니다")
            if self.processed_at_ms < self.decision_at_ms:
                raise RecordError(
                    f"processed_at_ms({self.processed_at_ms})가 decision_at_ms({self.decision_at_ms})보다 앞섭니다"
                )
        elif self.processed_at_ms is not None:
            raise RecordError(f"status={self.status.value}이면 processed_at_ms는 null이어야 합니다")


@dataclass(frozen=True, slots=True)
class Rule:
    """제한 규칙. 같은 수치도 알고리즘별로 의미가 다르다(configs/rules.yaml 주석 참고)."""

    rule_id: str
    scope: Scope
    limit: int
    window_ms: int
    bucket_capacity: int
    refill_per_sec: float
    queue_capacity: int
    leak_per_sec: float
    idle_ttl_ms: int

    def __post_init__(self) -> None:
        _check_text("rule_id", self.rule_id)
        try:
            object.__setattr__(self, "scope", Scope(self.scope))
        except ValueError:
            allowed = ", ".join(s.value for s in Scope)
            raise RecordError(f"scope: {allowed} 중 하나여야 합니다 (값: {self.scope!r})") from None
        for name in ("limit", "window_ms", "bucket_capacity", "queue_capacity", "idle_ttl_ms"):
            _check_int(name, getattr(self, name), minimum=1)
        for name in ("refill_per_sec", "leak_per_sec"):
            object.__setattr__(self, name, _check_number(name, getattr(self, name), positive=True))


# ---------------------------------------------------------------------------
# CSV 직렬화

REQUEST_FIELDS: tuple[str, ...] = tuple(Request.__dataclass_fields__)
DECISION_FIELDS: tuple[str, ...] = tuple(Decision.__dataclass_fields__)

_REQUEST_INT_FIELDS = {"scheduled_at_ms"}
_DECISION_INT_FIELDS = {"arrival_at_ms", "decision_at_ms"}
_DECISION_NULLABLE_INT_FIELDS = {"processed_at_ms", "retry_after_ms", "remaining"}


def _parse_int(name: str, text: str) -> int:
    if not _INT_RE.fullmatch(text):
        raise RecordError(f"{name}: 정수여야 합니다 (단위 접미사·소수점 불가, 값: {text!r})")
    return int(text)


def _parse_float(name: str, text: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise RecordError(f"{name}: 숫자여야 합니다 (값: {text!r})") from None


def _format(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, float):
        return repr(value)
    return str(value)


def request_from_row(row: Mapping[str, str]) -> Request:
    values: dict[str, object] = {}
    for name in REQUEST_FIELDS:
        text = row[name]
        if text == "":
            raise RecordError(f"{name}: 필수 값이 비어 있습니다")
        values[name] = _parse_int(name, text) if name in _REQUEST_INT_FIELDS else text
    return Request(**values)  # type: ignore[arg-type]


def decision_from_row(row: Mapping[str, str]) -> Decision:
    values: dict[str, object] = {}
    for name in DECISION_FIELDS:
        text = row[name]
        if name in _DECISION_NULLABLE_INT_FIELDS:
            values[name] = None if text == "" else _parse_int(name, text)
            continue
        if text == "":
            raise RecordError(f"{name}: 필수 값이 비어 있습니다")
        if name in _DECISION_INT_FIELDS:
            values[name] = _parse_int(name, text)
        elif name == "latency_us":
            values[name] = _parse_float(name, text)
        else:
            values[name] = text
    return Decision(**values)  # type: ignore[arg-type]


def request_to_row(request: Request) -> dict[str, str]:
    return {name: _format(getattr(request, name)) for name in REQUEST_FIELDS}


def decision_to_row(decision: Decision) -> dict[str, str]:
    return {name: _format(getattr(decision, name)) for name in DECISION_FIELDS}


def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: Iterable[dict[str, str]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path, fieldnames: tuple[str, ...], parse) -> list:
    path = Path(path)
    records = []
    seen_ids: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            raise RecordError(f"{path}: 헤더가 없습니다")
        if tuple(header) != fieldnames:
            raise RecordError(f"{path}: 헤더가 명세와 다릅니다. 기대: {list(fieldnames)}, 실제: {header}")
        for values in reader:
            line = reader.line_num
            if len(values) != len(fieldnames):
                raise RecordError(f"{path}:{line}: 열 개수 {len(values)} != {len(fieldnames)}")
            try:
                record = parse(dict(zip(fieldnames, values)))
            except RecordError as exc:
                raise RecordError(f"{path}:{line}: {exc}") from None
            if record.request_id in seen_ids:
                raise RecordError(f"{path}:{line}: 중복 request_id {record.request_id!r}")
            seen_ids.add(record.request_id)
            records.append(record)
    return records


def write_requests(path: Path, requests: Iterable[Request]) -> None:
    _write_csv(path, REQUEST_FIELDS, (request_to_row(r) for r in requests))


def read_requests(path: Path) -> list[Request]:
    return _read_csv(path, REQUEST_FIELDS, request_from_row)


def write_decisions(path: Path, decisions: Iterable[Decision]) -> None:
    _write_csv(path, DECISION_FIELDS, (decision_to_row(d) for d in decisions))


def read_decisions(path: Path) -> list[Decision]:
    return _read_csv(path, DECISION_FIELDS, decision_from_row)
