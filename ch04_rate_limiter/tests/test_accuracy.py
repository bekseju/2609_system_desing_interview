"""2.8 strict sliding log 기준 false allow / false reject 계산."""

import csv
from functools import cache

import pytest

from rate_limit_lab.algorithms import ALGORITHMS
from rate_limit_lab.config import load_rules
from rate_limit_lab.load.replay import replay
from rate_limit_lab.load.scenarios import generate, load_scenarios
from rate_limit_lab.metrics.accuracy import CLASSES, POLICY_SEMANTICS, REFERENCE, classify, compare, write_accuracy_csv
from tests.test_replay import overload_short

RULES = load_rules()


@cache
def results(scenario: str):
    requests = overload_short() if scenario == "overload_short" else generate(load_scenarios()[scenario], 42).requests
    return requests, {n: replay(requests, RULES, n, measure_latency=False).decisions for n in ALGORITHMS}


def test_classify_table():
    assert classify(True, True) == "agree_accept"
    assert classify(False, False) == "agree_reject"
    assert classify(False, True) == "false_allow"
    assert classify(True, False) == "false_reject"


def test_boundary_counts():
    _, decisions = results("boundary")
    ref = decisions[REFERENCE]
    counter, _ = compare(ref, decisions["sliding_counter"], "sliding_counter")
    fixed, _ = compare(ref, decisions["fixed_window"], "fixed_window")
    same, _ = compare(ref, ref, REFERENCE)
    # 1010ms 요청 중 sliding counter는 1건(추정 9.9 < 10), fixed window는 10건을 기준과 달리 허용
    assert (counter["false_allow"], counter["false_reject"]) == (1, 0)
    assert counter["false_allow_rate"] == 0.05  # 1 / 20
    assert counter["false_allow_rate_of_reference_rejected"] == 0.1  # 1 / 10
    assert (fixed["false_allow"], fixed["false_reject"]) == (10, 0)
    assert (same["false_allow"], same["false_reject"], same["agreement_rate"]) == (0, 0, 1.0)


@pytest.mark.parametrize("name", sorted(set(ALGORITHMS) - {REFERENCE}))
def test_classes_sum_to_total_and_semantics_labeled(name):
    requests, decisions = results("overload_short")
    summary, classes = compare(decisions[REFERENCE], decisions[name], name)
    assert len(classes) == len(requests) == summary["total"]
    assert sum(summary[c] for c in CLASSES) == summary["total"]
    assert summary["policy_semantics"] == POLICY_SEMANTICS[name]
    expected = "approximation_error" if name == "sliding_counter" else "policy_difference"
    assert summary["interpretation"] == expected


def test_sliding_counter_errors_under_overload():
    _, decisions = results("overload_short")
    summary, _ = compare(decisions[REFERENCE], decisions["sliding_counter"], "sliding_counter")
    assert summary["false_allow"] > 0 and summary["false_reject"] > 0
    assert 0 < summary["false_allow_rate"] < 0.2


def test_pdf_log_never_allows_more_than_strict():
    # PDF 로그는 창 안의 '모든' 요청을 기록하므로 기록 수가 항상 strict 이상 → false allow가 없다
    _, decisions = results("overload_short")
    summary, _ = compare(decisions[REFERENCE], decisions["sliding_log_pdf"], "sliding_log_pdf")
    assert summary["false_allow"] == 0 and summary["false_reject"] > 0


def test_token_and_leaking_bucket_accept_the_same_requests():
    # 같은 용량·속도면 토큰 수 = 용량 - 줄 길이 관계라 받아들이는 요청이 같다 (차이는 처리 시각뿐)
    _, decisions = results("overload_short")
    token = [d.status.value != "rejected" for d in decisions["token_bucket"]]
    leaking = [d.status.value != "rejected" for d in decisions["leaking_bucket"]]
    assert token == leaking


def test_compare_requires_same_requests():
    _, decisions = results("boundary")
    with pytest.raises(ValueError, match="요청 목록"):
        compare(decisions[REFERENCE], decisions["fixed_window"][:-1], "fixed_window")


def test_accuracy_csv(tmp_path):
    requests, decisions = results("boundary")
    classes = {n: compare(decisions[REFERENCE], d, n)[1] for n, d in decisions.items() if n != REFERENCE}
    path = tmp_path / "accuracy.csv"
    write_accuracy_csv(path, requests, decisions, classes)
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == 20
    late = [r for r in rows if r["scheduled_at_ms"] == "1010"]
    assert [r["sliding_counter_class"] for r in late].count("false_allow") == 1
    assert all(r["fixed_window_class"] == "false_allow" for r in late)
