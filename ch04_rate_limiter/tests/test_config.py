"""0.3 기본 정책 정의와 규칙 파일 읽기 오류."""

import pytest
import yaml

from rate_limit_lab.config import DEFAULT_RULES_PATH, RuleConfigError, load_rules, parse_rules
from rate_limit_lab.models import Scope
from tests.conftest import run_python


def _valid_rule(**overrides):
    rule = dict(
        rule_id="r1",
        scope="user",
        limit=10,
        window_ms=1000,
        bucket_capacity=10,
        refill_per_sec=10,
        queue_capacity=10,
        leak_per_sec=10,
        idle_ttl_ms=2000,
    )
    rule.update(overrides)
    return rule


def test_default_policy_values():
    rules = load_rules(DEFAULT_RULES_PATH)
    rule = rules["user_default"]
    assert rule.scope is Scope.USER
    assert (rule.limit, rule.window_ms) == (10, 1000)
    assert rule.bucket_capacity == 10 and rule.queue_capacity == 10
    assert rule.refill_per_sec == 10.0 and rule.leak_per_sec == 10.0


def test_missing_file(tmp_path):
    with pytest.raises(RuleConfigError, match="파일을 읽을 수 없습니다"):
        load_rules(tmp_path / "nope.yaml")


def test_yaml_syntax_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("version: 1\nrules: [\n", encoding="utf-8")
    with pytest.raises(RuleConfigError, match="YAML 구문 오류"):
        load_rules(path)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (None, "최상위는 매핑"),
        ([], "최상위는 매핑"),
        ({"version": 2, "rules": [_valid_rule()]}, "version은 1"),
        ({"version": 1}, "rules는 비어 있지 않은 목록"),
        ({"version": 1, "rules": []}, "rules는 비어 있지 않은 목록"),
        ({"version": 1, "rules": [_valid_rule()], "extra": 1}, "알 수 없는 최상위 키"),
        ({"version": 1, "rules": ["r1"]}, r"rules\[0\]: 매핑"),
    ],
)
def test_structure_errors(data, message):
    with pytest.raises(RuleConfigError, match=message):
        parse_rules(data)


def _without(key):
    rule = _valid_rule()
    del rule[key]
    return rule


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (_without("limit"), r"필수 필드 누락 \['limit'\]"),
        (_valid_rule(burst=5), r"알 수 없는 필드 \['burst'\]"),
        (_valid_rule(scope="tenant"), "scope: global, user, ip, endpoint"),
        (_valid_rule(limit=0), "limit: 1 이상"),
        (_valid_rule(limit=-1), "limit: 1 이상"),
        (_valid_rule(limit="10"), "limit: 정수가 필요"),
        (_valid_rule(limit=True), "limit: 정수가 필요"),
        (_valid_rule(window_ms=1.5), "window_ms: 정수가 필요"),
        (_valid_rule(window_ms="1s"), "window_ms: 정수가 필요"),
        (_valid_rule(refill_per_sec=0), "refill_per_sec: 0보다 커야"),
        (_valid_rule(leak_per_sec=float("inf")), "leak_per_sec: 유한한 값"),
        (_valid_rule(rule_id=""), "rule_id: 비어 있지 않은 문자열"),
    ],
)
def test_rule_field_errors(rule, message):
    with pytest.raises(RuleConfigError, match=message) as excinfo:
        parse_rules({"version": 1, "rules": [rule]})
    assert "rules[0]" in str(excinfo.value)


def test_duplicate_rule_id():
    with pytest.raises(RuleConfigError, match=r"rules\[1\]: 중복 rule_id 'r1'"):
        parse_rules({"version": 1, "rules": [_valid_rule(), _valid_rule()]})


def test_fractional_rate_allowed():
    rules = parse_rules({"version": 1, "rules": [_valid_rule(refill_per_sec=0.5)]})
    assert rules["r1"].refill_per_sec == 0.5


def test_cli_reports_config_error(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "rules": [_valid_rule(limit=0)]}), encoding="utf-8")
    result = run_python("-m", "rate_limit_lab", "rules", str(path))
    assert result.returncode == 2
    assert "limit: 1 이상" in result.stderr
