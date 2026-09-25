"""정책 키 생성: `rule_id + scope + 대상`을 명시적으로 인코딩한다.

키 형식: ``rl:<rule_id>:<scope>:<target>``. rule_id와 대상 값은 퍼센트 인코딩되어
구분자 `:`가 값 안에 나타나지 않으므로, 서로 다른 (rule_id, scope, target)는
항상 서로 다른 키가 된다. 전역 규칙의 대상은 인코딩 결과에 나올 수 없는 ``*``이다
(`quote`는 `*`를 `%2A`로 바꾼다).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from urllib.parse import quote, unquote

from rate_limit_lab.models import Request, Rule, Scope

KEY_PREFIX = "rl"
GLOBAL_TARGET = "*"


class RuleSelectionError(LookupError):
    """요청에 맞는 규칙을 찾을 수 없을 때 발생한다."""


def _enc(part: str) -> str:
    return quote(part, safe="")


def target_of(scope: Scope, request: Request) -> str:
    """scope에 따라 요청에서 집계 대상 값을 고른다."""
    match scope:
        case Scope.GLOBAL:
            return GLOBAL_TARGET
        case Scope.USER:
            return request.client_id
        case Scope.IP:
            return request.ip
        case Scope.ENDPOINT:
            return request.endpoint
    raise ValueError(f"지원하지 않는 scope: {scope!r}")


def make_key(rule: Rule, request: Request) -> str:
    target = GLOBAL_TARGET if rule.scope is Scope.GLOBAL else _enc(target_of(rule.scope, request))
    return f"{KEY_PREFIX}:{_enc(rule.rule_id)}:{rule.scope.value}:{target}"


def parse_key(key: str) -> tuple[str, Scope, str]:
    """`make_key`의 역변환. 디버깅·보고서용."""
    prefix, rule_id, scope, target = key.split(":")
    if prefix != KEY_PREFIX:
        raise ValueError(f"알 수 없는 키 접두사: {key!r}")
    return unquote(rule_id), Scope(scope), target if target == GLOBAL_TARGET else unquote(target)


def select_rules(request: Request, rules: Mapping[str, Rule]) -> list[Rule]:
    """요청에 적용할 규칙 목록. MVP에서는 요청의 `rule_id` 하나를 선택한다.

    복수 규칙 결합(모든 규칙이 허용해야 허용)은 7단계에서 검증한다.
    """
    rule = rules.get(request.rule_id)
    if rule is None:
        raise RuleSelectionError(f"request {request.request_id}: 알 수 없는 rule_id {request.rule_id!r}")
    return [rule]


def keys_for(request: Request, rules: Iterable[Rule]) -> list[tuple[Rule, str]]:
    return [(rule, make_key(rule, request)) for rule in rules]
