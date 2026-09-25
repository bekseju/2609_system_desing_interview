"""`configs/rules.yaml` 읽기와 검증."""

from __future__ import annotations

from pathlib import Path

import yaml

from rate_limit_lab.models import RecordError, Rule

DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "configs" / "rules.yaml"

SUPPORTED_VERSION = 1
RULE_FIELDS: tuple[str, ...] = tuple(Rule.__dataclass_fields__)


class RuleConfigError(ValueError):
    """규칙 파일을 읽을 수 없거나 내용이 잘못되었을 때 발생한다."""


def parse_rules(data: object, source: str = "<rules>") -> dict[str, Rule]:
    """YAML에서 읽은 객체를 검증하고 `rule_id -> Rule` 사전을 돌려준다(파일 순서 유지)."""
    if not isinstance(data, dict):
        raise RuleConfigError(f"{source}: 최상위는 매핑이어야 합니다")
    unknown_top = set(data) - {"version", "rules"}
    if unknown_top:
        raise RuleConfigError(f"{source}: 알 수 없는 최상위 키 {sorted(unknown_top)}")
    if data.get("version") != SUPPORTED_VERSION:
        raise RuleConfigError(f"{source}: version은 {SUPPORTED_VERSION}이어야 합니다 (값: {data.get('version')!r})")
    items = data.get("rules")
    if not isinstance(items, list) or not items:
        raise RuleConfigError(f"{source}: rules는 비어 있지 않은 목록이어야 합니다")

    rules: dict[str, Rule] = {}
    for index, item in enumerate(items):
        where = f"{source}: rules[{index}]"
        if not isinstance(item, dict):
            raise RuleConfigError(f"{where}: 매핑이어야 합니다")
        missing = [name for name in RULE_FIELDS if name not in item]
        if missing:
            raise RuleConfigError(f"{where}: 필수 필드 누락 {missing}")
        unknown = sorted(set(item) - set(RULE_FIELDS))
        if unknown:
            raise RuleConfigError(f"{where}: 알 수 없는 필드 {unknown}")
        try:
            rule = Rule(**item)
        except RecordError as exc:
            raise RuleConfigError(f"{where}: {exc}") from None
        if rule.rule_id in rules:
            raise RuleConfigError(f"{where}: 중복 rule_id {rule.rule_id!r}")
        rules[rule.rule_id] = rule
    return rules


def load_rules(path: Path | str = DEFAULT_RULES_PATH) -> dict[str, Rule]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuleConfigError(f"{path}: 파일을 읽을 수 없습니다 ({exc.strerror})") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuleConfigError(f"{path}: YAML 구문 오류: {exc}") from None
    return parse_rules(data, str(path))
