"""시나리오 설정(`configs/scenarios.yaml`) 읽기와 seed 기반 요청 생성.

흐름:
    scenarios = load_scenarios()                    # 설정 읽기·검증
    generated = generate(scenarios["normal"], seed=42)  # 요청 목록 (재생 순서로 정렬, ID 부여)
    write_generated(out_dir, generated)             # requests.csv + scenario.json

재현성 규칙:
- 난수는 `random.Random("<seed>|<구성 요소 순번>|...")`로 만든다. 문자열 seed는 해시 무작위화
  (PYTHONHASHSEED)의 영향을 받지 않으므로, 같은 seed면 어느 프로세스에서든 같은 요청이 나온다.
- 사용자마다 난수 흐름이 따로라서, 한 사용자의 설정을 바꿔도 다른 사용자의 요청은 그대로다.
- Poisson 도착 시각은 연속 시간으로 뽑은 뒤 정수 ms로 내림한다(같은 ms에 여러 건 가능).
- request_id는 재생 순서(시각, 구성 요소, 사용자, 순번)대로 r00000001부터 0을 채워 붙인다.
  그래서 `request_id` 문자열 순서 = 같은 시각 안의 재생 순서다.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rate_limit_lab.models import Request, write_requests

DEFAULT_SCENARIOS_PATH = Path(__file__).resolve().parents[2] / "configs" / "scenarios.yaml"
GENERATOR_VERSION = 1
ID_WIDTH = 8

# 구성 요소 종류별 필드와 타입
COMPONENT_FIELDS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "poisson": {
        "client_prefix": str, "users": int, "rate_per_user": (int, float), "start_ms": int, "end_ms": int,
    },
    "burst": {"client_id": str, "at_ms": int, "count": int},
    "unique_keys": {"client_prefix": str, "keys": int, "start_ms": int, "end_ms": int},
}
DEFAULT_FIELDS = ("seed", "rule_id", "endpoint", "method", "instance_id")


class ScenarioConfigError(ValueError):
    """시나리오 설정이 잘못되었을 때 발생한다."""


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    duration_ms: int
    components: tuple[dict[str, Any], ...]
    seed: int
    rule_id: str
    endpoint: str
    method: str
    instance_id: str


@dataclass(frozen=True)
class GeneratedScenario:
    scenario: Scenario
    seed: int
    requests: list[Request]  # 재생 순서
    component_counts: list[int]  # 구성 요소별 생성 건수


# ---------------------------------------------------------------------------
# 설정 읽기


def _check_type(where: str, name: str, value: object, expected: type | tuple[type, ...]) -> None:
    if isinstance(value, bool) or not isinstance(value, expected):
        raise ScenarioConfigError(f"{where}: {name}의 타입이 잘못되었습니다 (값: {value!r})")


def _parse_component(where: str, comp: object, duration_ms: int) -> dict[str, Any]:
    if not isinstance(comp, dict):
        raise ScenarioConfigError(f"{where}: 매핑이어야 합니다")
    kind = comp.get("type")
    if kind not in COMPONENT_FIELDS:
        raise ScenarioConfigError(f"{where}: type은 {sorted(COMPONENT_FIELDS)} 중 하나여야 합니다 (값: {kind!r})")
    fields = COMPONENT_FIELDS[kind]
    missing = [f for f in fields if f not in comp]
    unknown = sorted(set(comp) - set(fields) - {"type"})
    if missing:
        raise ScenarioConfigError(f"{where}: 필수 필드 누락 {missing}")
    if unknown:
        raise ScenarioConfigError(f"{where}: 알 수 없는 필드 {unknown}")
    for name, expected in fields.items():
        _check_type(where, name, comp[name], expected)

    for name in ("users", "keys", "count"):
        if name in comp and comp[name] < 1:
            raise ScenarioConfigError(f"{where}: {name}는 1 이상이어야 합니다")
    if kind == "poisson" and comp["rate_per_user"] <= 0:
        raise ScenarioConfigError(f"{where}: rate_per_user는 0보다 커야 합니다")
    if "start_ms" in comp and not (0 <= comp["start_ms"] < comp["end_ms"] <= duration_ms):
        raise ScenarioConfigError(f"{where}: 0 <= start_ms < end_ms <= duration_ms({duration_ms})여야 합니다")
    if "at_ms" in comp and not (0 <= comp["at_ms"] < duration_ms):
        raise ScenarioConfigError(f"{where}: 0 <= at_ms < duration_ms({duration_ms})여야 합니다")
    return dict(comp)


def parse_scenarios(data: object, source: str = "<scenarios>") -> dict[str, Scenario]:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ScenarioConfigError(f"{source}: 최상위는 version: 1을 가진 매핑이어야 합니다")
    defaults = data.get("defaults")
    if not isinstance(defaults, dict) or sorted(defaults) != sorted(DEFAULT_FIELDS):
        raise ScenarioConfigError(f"{source}: defaults에는 {list(DEFAULT_FIELDS)}가 모두 있어야 합니다")
    _check_type(f"{source}: defaults", "seed", defaults["seed"], int)
    for name in DEFAULT_FIELDS[1:]:
        _check_type(f"{source}: defaults", name, defaults[name], str)

    items = data.get("scenarios")
    if not isinstance(items, dict) or not items:
        raise ScenarioConfigError(f"{source}: scenarios는 비어 있지 않은 매핑이어야 합니다")

    scenarios: dict[str, Scenario] = {}
    for name, body in items.items():
        where = f"{source}: scenarios.{name}"
        if not isinstance(body, dict):
            raise ScenarioConfigError(f"{where}: 매핑이어야 합니다")
        unknown = sorted(set(body) - {"description", "duration_ms", "components"})
        if unknown:
            raise ScenarioConfigError(f"{where}: 알 수 없는 필드 {unknown}")
        duration = body.get("duration_ms")
        _check_type(where, "duration_ms", duration, int)
        if duration <= 0:
            raise ScenarioConfigError(f"{where}: duration_ms는 양수여야 합니다")
        components = body.get("components")
        if not isinstance(components, list) or not components:
            raise ScenarioConfigError(f"{where}: components는 비어 있지 않은 목록이어야 합니다")
        scenarios[name] = Scenario(
            name=name,
            description=str(body.get("description", "")),
            duration_ms=duration,
            components=tuple(
                _parse_component(f"{where}.components[{i}]", c, duration) for i, c in enumerate(components)
            ),
            **defaults,
        )
    return scenarios


def load_scenarios(path: Path | str = DEFAULT_SCENARIOS_PATH) -> dict[str, Scenario]:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScenarioConfigError(f"{path}: 파일을 읽을 수 없습니다 ({exc.strerror})") from None
    except yaml.YAMLError as exc:
        raise ScenarioConfigError(f"{path}: YAML 구문 오류: {exc}") from None
    return parse_scenarios(data, str(path))


# ---------------------------------------------------------------------------
# 생성

# (시각, 구성 요소 순번, 사용자 순번, 사용자 내 순번, client_id) — 이 순서가 곧 재생 순서
_Arrival = tuple[int, int, int, int, str]


def _client_ids(prefix: str, n: int) -> list[str]:
    width = max(3, len(str(n - 1)))
    return [f"{prefix}-{i:0{width}d}" for i in range(n)]


def _poisson(comp: dict[str, Any], index: int, seed: int) -> list[_Arrival]:
    rate_per_ms = comp["rate_per_user"] / 1000
    start, end = comp["start_ms"], comp["end_ms"]
    arrivals: list[_Arrival] = []
    for user, client_id in enumerate(_client_ids(comp["client_prefix"], comp["users"])):
        rng = random.Random(f"{seed}|{index}|poisson|{user}")
        t, seq = float(start), 0
        while True:
            t += rng.expovariate(rate_per_ms)  # 다음 도착까지의 간격 ~ 지수 분포
            if t >= end:
                break
            arrivals.append((int(t), index, user, seq, client_id))
            seq += 1
    return arrivals


def _burst(comp: dict[str, Any], index: int, seed: int) -> list[_Arrival]:
    return [(comp["at_ms"], index, 0, seq, comp["client_id"]) for seq in range(comp["count"])]


def _unique_keys(comp: dict[str, Any], index: int, seed: int) -> list[_Arrival]:
    rng = random.Random(f"{seed}|{index}|unique_keys")
    ids = _client_ids(comp["client_prefix"], comp["keys"])
    return [(rng.randrange(comp["start_ms"], comp["end_ms"]), index, k, 0, cid) for k, cid in enumerate(ids)]


_GENERATORS = {"poisson": _poisson, "burst": _burst, "unique_keys": _unique_keys}


def _ip_for(n: int) -> str:
    return f"10.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}"


def generate(scenario: Scenario, seed: int | None = None) -> GeneratedScenario:
    seed = scenario.seed if seed is None else seed
    arrivals: list[_Arrival] = []
    counts: list[int] = []
    for index, comp in enumerate(scenario.components):
        part = _GENERATORS[comp["type"]](comp, index, seed)
        counts.append(len(part))
        arrivals.extend(part)
    arrivals.sort()

    # 클라이언트마다 고정 IP (구성 요소 순서대로 처음 등장한 순서로 배정)
    ips: dict[str, str] = {}
    for index, comp in enumerate(scenario.components):
        if comp["type"] == "burst":
            names = [comp["client_id"]]
        else:
            names = _client_ids(comp["client_prefix"], comp.get("users", comp.get("keys")))
        for name in names:
            ips.setdefault(name, _ip_for(len(ips) + 1))

    requests = [
        Request(
            request_id=f"r{i:0{ID_WIDTH}d}",
            scheduled_at_ms=t,
            client_id=client_id,
            ip=ips[client_id],
            endpoint=scenario.endpoint,
            method=scenario.method,
            rule_id=scenario.rule_id,
            instance_id=scenario.instance_id,
        )
        for i, (t, _, _, _, client_id) in enumerate(arrivals, start=1)
    ]
    return GeneratedScenario(scenario, seed, requests, counts)


# ---------------------------------------------------------------------------
# 메타데이터


def _expected_in(comp: dict[str, Any], lo: int, hi: int) -> float:
    """구간 [lo, hi)에서 이 구성 요소가 만들 것으로 기대되는 요청 수."""
    if comp["type"] == "burst":
        return comp["count"] if lo <= comp["at_ms"] < hi else 0.0
    overlap = max(0, min(hi, comp["end_ms"]) - max(lo, comp["start_ms"]))
    if comp["type"] == "poisson":
        return comp["users"] * comp["rate_per_user"] * overlap / 1000
    return comp["keys"] * overlap / (comp["end_ms"] - comp["start_ms"])


def build_metadata(generated: GeneratedScenario, bin_ms: int = 1000) -> dict[str, Any]:
    scenario, requests = generated.scenario, generated.requests
    duration = scenario.duration_ms
    actual = Counter(r.scheduled_at_ms // bin_ms for r in requests)
    bins = []
    for start in range(0, duration, bin_ms):
        end = min(start + bin_ms, duration)
        seconds = (end - start) / 1000
        target = sum(_expected_in(c, start, end) for c in scenario.components)
        bins.append({
            "start_ms": start,
            "end_ms": end,
            "target_rps": round(target / seconds, 3),
            "actual_rps": round(actual.get(start // bin_ms, 0) / seconds, 3),
        })
    per_client = Counter(r.client_id for r in requests)
    expected_total = sum(_expected_in(c, 0, duration) for c in scenario.components)
    return {
        "scenario": scenario.name,
        "description": scenario.description,
        "seed": generated.seed,
        "generator_version": GENERATOR_VERSION,
        "rule_id": scenario.rule_id,
        "duration_ms": duration,
        "components": [
            {**comp, "generated": count, "expected": round(_expected_in(comp, 0, duration), 3)}
            for comp, count in zip(scenario.components, generated.component_counts)
        ],
        "total_requests": len(requests),
        "expected_total": round(expected_total, 3),
        "target_rps_overall": round(expected_total / (duration / 1000), 3),
        "actual_rps_overall": round(len(requests) / (duration / 1000), 3),
        "unique_clients": len(per_client),
        "requests_per_client": {
            "min": min(per_client.values(), default=0),
            "max": max(per_client.values(), default=0),
            "mean": round(len(requests) / len(per_client), 3) if per_client else 0,
        },
        "max_requests_same_ms": max(Counter(r.scheduled_at_ms for r in requests).values(), default=0),
        "rps_bins": bins,
    }


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_generated(out_dir: Path | str, generated: GeneratedScenario) -> dict[str, Any]:
    """`requests.csv`와 `scenario.json`(메타데이터 + CSV 해시)을 쓴다."""
    out_dir = Path(out_dir)
    csv_path = out_dir / "requests.csv"
    write_requests(csv_path, generated.requests)
    metadata = build_metadata(generated)
    metadata["requests_csv_sha256"] = sha256_of(csv_path)
    (out_dir / "scenario.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata
