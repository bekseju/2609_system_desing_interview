"""0.5 정책 키 생성, scope별 대상 선택, 키 충돌·사용자 격리."""

import itertools

import pytest

from rate_limit_lab.keys import GLOBAL_TARGET, RuleSelectionError, keys_for, make_key, parse_key, select_rules
from rate_limit_lab.models import Scope
from tests.conftest import make_request, make_rule


def test_key_encodes_rule_scope_target():
    request = make_request(client_id="alice", ip="10.0.0.1", endpoint="/work")
    assert make_key(make_rule("r", Scope.USER), request) == "rl:r:user:alice"
    assert make_key(make_rule("r", Scope.IP), request) == "rl:r:ip:10.0.0.1"
    assert make_key(make_rule("r", Scope.ENDPOINT), request) == "rl:r:endpoint:%2Fwork"
    assert make_key(make_rule("r", Scope.GLOBAL), request) == "rl:r:global:*"


def test_global_ignores_request_attributes():
    rule = make_rule("g", Scope.GLOBAL)
    a = make_request(client_id="a", ip="1.1.1.1", endpoint="/x")
    b = make_request(client_id="b", ip="2.2.2.2", endpoint="/y")
    assert make_key(rule, a) == make_key(rule, b)


def test_user_isolation_and_same_user_shares_key():
    rule = make_rule("u", Scope.USER)
    alice1 = make_request("r1", client_id="alice", ip="1.1.1.1")
    alice2 = make_request("r2", client_id="alice", ip="2.2.2.2")
    bob = make_request("r3", client_id="bob", ip="1.1.1.1")
    assert make_key(rule, alice1) == make_key(rule, alice2)
    assert make_key(rule, alice1) != make_key(rule, bob)


def test_same_target_different_rule_or_scope_do_not_collide():
    request = make_request(client_id="x", ip="x", endpoint="x")
    keys = {make_key(make_rule(rid, scope), request) for rid in ("a", "b") for scope in Scope}
    assert len(keys) == 2 * len(Scope)


def test_delimiter_injection_does_not_collide():
    # 인코딩이 없다면 "a:user" + "b" 와 "a" + "user:b" 가 같은 문자열이 될 수 있다.
    cases = [
        (make_rule("a:user", Scope.USER), make_request(client_id="b")),
        (make_rule("a", Scope.USER), make_request(client_id="user:b")),
        (make_rule("a", Scope.USER), make_request(client_id="*")),
        (make_rule("a", Scope.GLOBAL), make_request()),
        (make_rule("a", Scope.USER), make_request(client_id="%2A")),
        (make_rule("a", Scope.USER), make_request(client_id="a b")),
        (make_rule("a", Scope.USER), make_request(client_id="a%20b")),
    ]
    keys = [make_key(rule, request) for rule, request in cases]
    assert len(set(keys)) == len(keys)


@pytest.mark.parametrize(
    "client_id", ["alice", "a:b", "*", "%2A", "사용자", "a/b?c=d", " spaced "],
)
def test_parse_key_roundtrip(client_id):
    rule = make_rule("rule:1", Scope.USER)
    assert parse_key(make_key(rule, make_request(client_id=client_id))) == ("rule:1", Scope.USER, client_id)
    assert parse_key(make_key(make_rule("g", Scope.GLOBAL), make_request())) == ("g", Scope.GLOBAL, GLOBAL_TARGET)


def test_many_users_unique_keys():
    rule = make_rule()
    users = [f"user-{i}" for i in range(10_000)] + [f"user:{i}" for i in range(100)]
    keys = {make_key(rule, make_request(client_id=u)) for u in users}
    assert len(keys) == len(users)


def test_select_rules_by_request_rule_id():
    rules = {r.rule_id: r for r in (make_rule("user_default"), make_rule("ip_rule", Scope.IP))}
    request = make_request(rule_id="ip_rule", ip="9.9.9.9")
    selected = select_rules(request, rules)
    assert [r.rule_id for r in selected] == ["ip_rule"]
    assert keys_for(request, selected) == [(rules["ip_rule"], "rl:ip_rule:ip:9.9.9.9")]


def test_select_rules_unknown_rule():
    with pytest.raises(RuleSelectionError, match="알 수 없는 rule_id 'missing'"):
        select_rules(make_request(rule_id="missing"), {"user_default": make_rule()})


def test_scope_selection_distinct_per_attribute():
    # 같은 요청 집합을 scope별로 묶었을 때 그룹 수가 해당 속성의 고유값 수와 같다.
    requests = [
        make_request(f"r{i}", client_id=f"u{i % 3}", ip=f"ip{i % 5}", endpoint=f"/e{i % 2}")
        for i, _ in zip(itertools.count(), range(30))
    ]
    expected = {Scope.USER: 3, Scope.IP: 5, Scope.ENDPOINT: 2, Scope.GLOBAL: 1}
    for scope, count in expected.items():
        rule = make_rule("r", scope)
        assert len({make_key(rule, r) for r in requests}) == count
