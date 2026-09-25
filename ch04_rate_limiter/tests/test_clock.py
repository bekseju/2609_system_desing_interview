"""0.6 가상 시계, [start,end) 구간, 동일 시각 정렬, 단조 시계."""

import random

import pytest

from rate_limit_lab.clock import (
    ClockError,
    Interval,
    MonotonicClock,
    VirtualClock,
    bucket_of,
    replay_order,
    window_index,
)
from rate_limit_lab.models import write_requests
from tests.conftest import make_request, run_python


def test_virtual_clock_advances_and_rejects_backward():
    clock = VirtualClock()
    assert clock.now_ms() == 0
    assert clock.advance_to(990) == 990
    assert clock.advance_to(990) == 990  # 같은 시각 허용
    assert clock.advance_by(20) == 1010
    with pytest.raises(ClockError, match="시계 역행"):
        clock.advance_to(1009)
    assert clock.now_ms() == 1010


@pytest.mark.parametrize("bad", [1.5, "10", True, None])
def test_virtual_clock_requires_int_ms(bad):
    with pytest.raises(ClockError, match="정수 ms"):
        VirtualClock().advance_to(bad)


def test_interval_half_open():
    interval = Interval(1000, 2000)
    assert not interval.contains(999)
    assert interval.contains(1000)
    assert interval.contains(1999)
    assert not interval.contains(2000)
    assert interval.duration_ms == 1000
    with pytest.raises(ClockError, match="빈 구간"):
        Interval(5, 5)


@pytest.mark.parametrize(
    ("t", "width", "index"),
    [(0, 1000, 0), (999, 1000, 0), (1000, 1000, 1), (1010, 1000, 1), (99, 100, 0), (100, 100, 1)],
)
def test_window_index_and_bucket(t, width, index):
    assert window_index(t, width) == index
    bucket = bucket_of(t, width)
    assert bucket == Interval(index * width, (index + 1) * width)
    assert bucket.contains(t)


def test_window_index_rejects_non_positive_width():
    with pytest.raises(ClockError):
        window_index(5, 0)


def _sample_requests():
    # 같은 시각(990ms, 1010ms)에 여러 요청이 몰린 경계 사례 형태.
    ids = iter(range(1, 1000))
    return [make_request(f"r{next(ids):06d}", t) for t in [990] * 10 + [1010] * 10 + [0, 5, 5, 2000]]


def test_replay_order_is_time_then_request_id():
    ordered = replay_order(_sample_requests())
    keys = [(r.scheduled_at_ms, r.request_id) for r in ordered]
    assert keys == sorted(keys)


def test_replay_order_stable_under_shuffles():
    base = _sample_requests()
    expected = [r.request_id for r in replay_order(base)]
    for seed in range(20):
        shuffled = base[:]
        random.Random(seed).shuffle(shuffled)
        assert [r.request_id for r in replay_order(shuffled)] == expected


def test_replay_order_same_across_processes(tmp_path):
    requests = _sample_requests()
    random.Random(7).shuffle(requests)
    path = tmp_path / "requests.csv"
    write_requests(path, requests)
    code = (
        "import sys; from rate_limit_lab.models import read_requests; from rate_limit_lab.clock import replay_order;"
        "print(','.join(r.request_id for r in replay_order(read_requests(sys.argv[1]))))"
    )
    outputs = set()
    for hash_seed in ("0", "1", "12345"):
        result = run_python("-c", code, str(path), env={"PYTHONHASHSEED": hash_seed})
        assert result.returncode == 0, result.stderr
        outputs.add(result.stdout.strip())
    assert outputs == {",".join(r.request_id for r in replay_order(requests))}


def test_monotonic_clock_non_decreasing():
    clock = MonotonicClock()
    values = [clock.now_ns() for _ in range(1000)]
    assert values[0] >= 0
    assert all(a <= b for a, b in zip(values, values[1:]))
    assert isinstance(clock.now_ms(), int)
