"""실험 폴더에서 comparison.csv, 그래프(PNG), report.md를 만든다 (TODO 3.6).

    python -m rate_limit_lab.metrics.report results/<UTC timestamp>

실험 폴더 구조: <exp>/<scenario>/<algorithm>/<backend>/<replicate>/summary.json ...
비교 원칙:
- 같은 시나리오(같은 입력 CSV)·같은 규칙·같은 백엔드끼리만 한 표에 놓는다.
- 측정 범위(런타임/프로세스/HTTP)가 다른 지표는 서로 다른 열·그래프로 분리한다.
- 계획했지만 결과가 없는 조합은 `not_run`으로 표시한다(experiment.json의 plan 기준).
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from rate_limit_lab.metrics.accuracy import POLICY_SEMANTICS, REFERENCE, compare  # noqa: E402
from rate_limit_lab.metrics.summary import ConsistencyError, check_consistency  # noqa: E402
from rate_limit_lab.models import read_request_results  # noqa: E402

ALGORITHM_ORDER = ["sliding_log", "sliding_counter", "fixed_window", "sliding_log_pdf", "token_bucket",
                   "leaking_bucket"]
# 색은 알고리즘(엔터티)에 고정한다. dataviz 기본 팔레트 1~6번 (검증: 인접 CVD ΔE ≥ 9.1)
COLORS = dict(zip(ALGORITHM_ORDER, ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]))
INK, INK_2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
SKIP_KEYS = {"rules", "units", "input_csv", "input_sha256", "moving_window.max_accepted_key"}

SCOPES = [
    ("판정 지연 (decision_latency_us)", "µs", "decide()/admit() 호출 전후, 알고리즘만 (로컬: 재생 1회차 / HTTP: 서버 내부)"),
    ("처리량 (throughput_rps)", "요청/초", "로컬: 입력 건수 ÷ 재생 루프 벽시계 시간 / HTTP: 받은 응답 ÷ 측정 시간"),
    ("큐 대기 (queue_wait_ms)", "ms", "누출 버킷만. 처리 완료 − 접수 (로컬: 가상 시계 / HTTP: 서버 단조 시계)"),
    ("이동 창 최대 (moving_window.*)", "건", "길이 1000ms 반열린 창 [s, s+1000)의 최대 건수"),
    ("알고리즘 상태 peak", "bytes", "Python 할당 peak, 기록 없이 판정만 반복한 3회차 동안 ≈ 알고리즘 상태 크기"),
    ("tracemalloc peak", "bytes", "Python 할당 peak, 재생 2회차 동안 (알고리즘 상태 + 재생기의 판정·이벤트 기록)"),
    ("RSS", "bytes", "프로세스 전체 상주 메모리 (인터프리터·라이브러리·입력 포함). tracemalloc과 직접 비교 금지"),
    ("HTTP 왕복 (rtt_ms)", "ms", "부하 발생기 관점: 요청 전송 시작 → 응답 수신 완료"),
    ("발송 지연 (send_lag_ms)", "ms", "부하 발생기 관점: 실제 전송 시각 − 예정 시각"),
]


# ---------------------------------------------------------------------------
# 수집


def find_summaries(exp_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    found = []
    for path in sorted(exp_dir.glob("*/*/*/*/summary.json")):
        found.append((path.parent, json.loads(path.read_text(encoding="utf-8"))))
    return found


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if name in SKIP_KEYS:
            continue
        if isinstance(value, dict):
            flat.update(flatten(value, f"{name}."))
        elif not isinstance(value, list):
            flat[name] = value
    return flat


def write_comparison(exp_dir: Path, summaries: list[tuple[Path, dict[str, Any]]]) -> Path:
    rows = [flatten(s) for _, s in summaries]
    head = ["scenario", "algorithm", "backend", "replicate"]
    columns = head + [c for c in dict.fromkeys(k for r in rows for k in r) if c not in head]
    path = exp_dir / "comparison.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    return path


def _median(values: list[float | None]) -> float | None:
    data = [v for v in values if v is not None]
    return statistics.median(data) if data else None


def _group(summaries):
    """(scenario, backend) -> algorithm -> [summary...]"""
    groups: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for _, s in summaries:
        groups[(s["scenario"], s["backend"])][s["algorithm"]].append(s)
    return groups


def _ordered(algorithms) -> list[str]:
    known = [a for a in ALGORITHM_ORDER if a in algorithms]
    return known + sorted(set(algorithms) - set(known))


# ---------------------------------------------------------------------------
# 그래프


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)


def _figure(nrows: int, ncols: int, width: float, height: float):
    fig, axes = plt.subplots(nrows, ncols, figsize=(width, height), squeeze=False, sharex=True, sharey=True)
    fig.patch.set_facecolor(SURFACE)
    return fig, axes


def plot_timeseries(exp_dir: Path, scenario: str, backend: str, runs: dict[str, list[dict[str, Any]]],
                    dirs: dict[tuple[str, str, str], Path]) -> Path | None:
    """알고리즘별 작은 그래프: 도착(회색) 대비 실제 수행(served) 건수."""
    algorithms = _ordered(runs)
    duration = runs[algorithms[0]][0].get("duration_ms", 60_000)
    bin_ms = 100 if duration <= 5_000 else 1000
    ncols = 3
    nrows = (len(algorithms) + ncols - 1) // ncols
    fig, axes = _figure(nrows, ncols, 11, 2.6 * nrows + 0.8)
    for ax in axes.flat:
        ax.set_visible(False)
    for ax, algorithm in zip(axes.flat, algorithms):
        rep_dir = dirs.get((scenario, algorithm, backend))
        if rep_dir is None or not (rep_dir / "timeseries.csv").exists():
            continue
        rows = [r for r in csv.DictReader((rep_dir / "timeseries.csv").open(encoding="utf-8"))
                if int(r["bin_ms"]) == bin_ms]
        x = [int(r["start_ms"]) / 1000 for r in rows]
        ax.set_visible(True)
        _style(ax)
        # 구간 건수이므로 계단(구간 시작부터 끝까지 같은 값)으로 그린다
        ax.step(x, [int(r["arrivals"]) for r in rows], where="post", color=AXIS, linewidth=2, label="arrivals")
        ax.step(x, [int(r["served"]) for r in rows], where="post", color=COLORS.get(algorithm, INK), linewidth=2,
                label="served (allowed now, or processed from queue)")
        ax.set_title(algorithm, color=INK, fontsize=10, loc="left")
    fig.suptitle(f"{scenario} ({backend}): requests per {bin_ms} ms bin",
                 color=INK, fontsize=11, x=0.01, ha="left")
    handles = [plt.Line2D([], [], color=AXIS, linewidth=2),
               plt.Line2D([], [], color=INK_2, linewidth=2)]
    fig.legend(handles, ["arrivals", "served (panel color): allowed now, or processed from queue"],
               loc="upper right", frameon=False, fontsize=8, labelcolor=INK_2, ncols=2)
    fig.supxlabel("time (s)", color=INK_2, fontsize=9)
    fig.supylabel(f"requests / {bin_ms} ms", color=INK_2, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    path = exp_dir / f"timeseries_{scenario}_{backend}.png"
    fig.savefig(path, dpi=120, facecolor=SURFACE)
    plt.close(fig)
    return path


def plot_metric_bars(exp_dir: Path, groups, backend: str, metric, title: str, unit: str, filename: str) -> Path | None:
    """시나리오마다 작은 가로 막대 그래프 (값을 막대 끝에 직접 표시)."""
    scenarios = [sc for (sc, be) in groups if be == backend]
    if not scenarios:
        return None
    fig, axes = plt.subplots(1, len(scenarios), figsize=(3.2 * len(scenarios) + 1.5, 3.2), squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    for ax, scenario in zip(axes[0], scenarios):
        runs = groups[(scenario, backend)]
        algorithms = _ordered(runs)
        values = [_median([metric(s) for s in runs[a]]) for a in algorithms]
        _style(ax)
        ax.grid(axis="y", visible=False)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        y = list(range(len(algorithms)))[::-1]
        ax.barh(y, [v or 0 for v in values], height=0.7, color=[COLORS.get(a, INK) for a in algorithms],
                edgecolor=SURFACE, linewidth=2)
        top = max([v for v in values if v is not None] or [1])
        for yi, v in zip(y, values):
            ax.text((v or 0) + top * 0.02, yi, "n/a" if v is None else f"{v:,.1f}", va="center", fontsize=7,
                    color=INK_2)
        ax.set_yticks(y, algorithms if ax is axes[0][0] else [""] * len(y), fontsize=8, color=INK_2)
        ax.set_xlim(0, top * 1.35)
        ax.set_title(scenario, color=INK, fontsize=10, loc="left")
    fig.suptitle(f"{title} ({backend}, median of replicates)", color=INK, fontsize=11, x=0.01, ha="left")
    fig.supxlabel(unit, color=INK_2, fontsize=9)
    fig.tight_layout()
    path = exp_dir / filename
    fig.savefig(path, dpi=120, facecolor=SURFACE)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# 보고서


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None or value == "":
        return "–"
    if isinstance(value, bool):
        return "yes" if value else "NO"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _local_table(runs: dict[str, list[dict[str, Any]]]) -> list[str]:
    lines = [
        "| 알고리즘 | 수락 | 거절 | 처리 완료 | 키당 이동 1초 최대 수락 | 키당 이동 1초 최대 수행 | 판정 p50/p95/p99 µs | 큐 대기 p50/p95/p99 ms "
        "| 상태 peak KB | tracemalloc peak MB | RSS peak MB | 활성 키 peak → 제거 후 | 회차 |",
        "|" + "---|" * 13,
    ]
    for a in _ordered(runs):
        ss = runs[a]
        s = ss[0]

        def med(path):
            return _median([_get(x, path) for x in ss])

        lat = "/".join(_fmt(med(f"decision_latency_us.{p}"), 2) for p in ("p50", "p95", "p99"))
        wait = "/".join(_fmt(med(f"queue_wait_ms.{p}"), 0) for p in ("p50", "p95", "p99"))
        mw = s.get("moving_window", {})
        lines.append(
            f"| {a} | {_fmt(s['accepted'])} | {_fmt(s['rejected'])} | {_fmt(s['status_counts']['processed'])} "
            f"| {_fmt(mw.get('max_accepted_per_key'))} | {_fmt(mw.get('max_served_per_key'))} | {lat} | {wait} "
            f"| {_fmt(_kb(med('memory.algorithm_state_peak_bytes')))} "
            f"| {_fmt(_mb(med('memory.tracemalloc_peak_bytes')))} | {_fmt(_mb(med('memory.rss_peak_bytes')))} "
            f"| {_fmt(s['state']['active_keys_peak'])} → {_fmt(s['state']['active_keys_end_after_evict'])} | {len(ss)} |"
        )
    return lines


def _mb(v):
    return None if v is None else v / 1e6


def _kb(v):
    return None if v is None else v / 1e3


def _get(data: dict[str, Any], dotted: str):
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _accuracy_lines(scenario: str, backend: str, dirs) -> list[str]:
    ref_dir = dirs.get((scenario, REFERENCE, backend))
    if ref_dir is None:
        return ["기준(sliding_log) 결과가 없어 비교하지 않음 (`not_run`)."]
    reference = [r.decision for r in read_request_results(ref_dir / "requests.csv")]
    lines = [
        "| 알고리즘 | 해석 | false allow (비율) | false reject (비율) | 정책 의미 |",
        "|---|---|---|---|---|",
    ]
    for (sc, algorithm, be), rep_dir in sorted(dirs.items()):
        if sc != scenario or be != backend or algorithm == REFERENCE:
            continue
        candidate = [r.decision for r in read_request_results(rep_dir / "requests.csv")]
        summary, _ = compare(reference, candidate, algorithm)
        lines.append(
            f"| {algorithm} | {summary['interpretation']} | {summary['false_allow']:,} ({summary['false_allow_rate']:.2%}) "
            f"| {summary['false_reject']:,} ({summary['false_reject_rate']:.2%}) | {summary['policy_semantics']} |"
        )
    return lines


def build_report(exp_dir: Path) -> Path:
    exp_dir = Path(exp_dir)
    summaries = find_summaries(exp_dir)
    experiment = {}
    if (exp_dir / "experiment.json").exists():
        experiment = json.loads((exp_dir / "experiment.json").read_text(encoding="utf-8"))
    write_comparison(exp_dir, summaries)
    groups = _group(summaries)
    # 그래프·정확도 비교에 쓰는 대표 회차 = 가장 작은 replicate 번호
    dirs: dict[tuple[str, str, str], Path] = {}
    for rep_dir, s in sorted(summaries, key=lambda x: x[1]["replicate"], reverse=True):
        dirs[(s["scenario"], s["algorithm"], s["backend"])] = rep_dir

    # 정합성 검사
    problems = []
    for rep_dir, s in summaries:
        try:
            check_consistency(s)
        except ConsistencyError as exc:
            problems.append(f"{rep_dir.relative_to(exp_dir)}: {exc}")
        if s.get("replay_consistent") is False:
            problems.append(f"{rep_dir.relative_to(exp_dir)}: 재생 1·2회차 판정 불일치")
    done = {(s["scenario"], s["algorithm"], s["backend"], s["replicate"]) for _, s in summaries}
    not_run = [tuple(p) for p in experiment.get("plan", []) if tuple(p) not in done]

    env = {}
    if summaries:
        env_path = summaries[0][0] / "environment.json"
        if env_path.exists():
            env = json.loads(env_path.read_text(encoding="utf-8"))

    md = [f"# Rate limiter 실험 보고서 — `{exp_dir.name}`", ""]
    if experiment:
        md += [f"- 실행 명령: `{' '.join(experiment.get('command', []))}`",
               f"- 시작(UTC): {experiment.get('started_at_utc', '–')}, seed: {experiment.get('seed', '–')}"]
    if env:
        m, o, p = env.get("machine", {}), env.get("os", {}), env.get("python", {})
        md += [f"- 환경: {m.get('cpu')} ({m.get('logical_cpus')} 논리 코어), {o.get('platform')}, Python {p.get('version')}"]
    md += [f"- 결과 {len(summaries)}개 회차, 정합성 문제 {len(problems)}건, 미실행(`not_run`) {len(not_run)}건", ""]

    md += ["## 읽는 법", "",
           "- 같은 수치(10건/초, 용량 10)라도 알고리즘마다 **정책 의미가 다르다**. 수락 비율만으로 우열을 매기지 않는다.",
           "- 한 표 안의 행은 같은 입력 CSV·같은 규칙·같은 백엔드다. 로컬(`memory`)과 HTTP 결과는 다른 표에 둔다.",
           "- 지연 값은 회차들의 중앙값이다. 판정·건수는 가상 시계 재생이라 회차마다 같다.",
           "- 판정 지연은 µs 단위라 운영체제 잡음의 영향을 크게 받는다. 요청이 적은 시나리오(boundary 20건, "
           "hot_key 100건)의 p95/p99는 몇 건의 값으로 정해지므로 알고리즘 비교에 쓰지 않는다.", "",
           "### 측정 범위와 단위", "", "| 지표 | 단위 | 범위 |", "|---|---|---|"]
    md += [f"| {a} | {b} | {c} |" for a, b, c in SCOPES]
    md += ["", "### 알고리즘별 정책 의미", "", "| 알고리즘 | 의미 |", "|---|---|"]
    md += [f"| {a} | {POLICY_SEMANTICS[a]} |" for a in ALGORITHM_ORDER]
    md.append("")

    figures = []
    local_backends = sorted({be for (_, be) in groups if be == "memory"})
    for backend in local_backends:
        md += [f"## 로컬 재생 결과 (`{backend}`)", ""]
        for (scenario, be), runs in groups.items():
            if be != backend:
                continue
            md += [f"### {scenario}", ""]
            md += _local_table(runs)
            md += ["", f"**strict sliding log 대비 요청별 판정 차이** (`{scenario}`)", ""]
            md += _accuracy_lines(scenario, backend, dirs)
            fig = plot_timeseries(exp_dir, scenario, backend, runs, dirs)
            if fig:
                figures.append(fig)
                md += ["", f"![{scenario} timeseries]({fig.name})"]
            md.append("")
        for args in (
            (lambda s: _get(s, "decision_latency_us.p95"), "Decision latency p95", "µs", f"latency_p95_{backend}.png"),
            (lambda s: _kb(_get(s, "memory.algorithm_state_peak_bytes")), "Algorithm state memory peak (tracemalloc)",
             "KB", f"memory_{backend}.png"),
        ):
            fig = plot_metric_bars(exp_dir, groups, backend, *args)
            if fig:
                figures.append(fig)
                md += [f"![{args[1]}]({fig.name})", ""]

    http_backends = sorted({be for (_, be) in groups if be.startswith("http")})
    if http_backends:
        md += ["## HTTP 부하 결과", "",
               "왕복 지연(rtt)·발송 지연(send lag)·서버 내부 판정 지연을 섞지 않고 따로 적는다. "
               "HTTP 결과는 벽시계 기반이라 회차마다 판정이 조금씩 다를 수 있다.", "",
               "| 시나리오 | 백엔드 | 알고리즘 | 회차 | 전송 | 200 | 202 | 429 | 타임아웃 | 전송 오류 | 기타 HTTP 오류 "
               "| rtt p50/p95/p99 ms | send lag p95/max ms | 서버 판정 p95 µs | 처리 완료 | 서버 RSS MB |",
               "|" + "---|" * 16]
        for (scenario, backend), runs in sorted(groups.items()):
            if not backend.startswith("http"):
                continue
            for a in _ordered(runs):
                for s in sorted(runs[a], key=lambda x: x["replicate"]):
                    o = s.get("outcomes", {})
                    rtt = "/".join(_fmt(_get(s, f"rtt_ms.{p}"), 2) for p in ("p50", "p95", "p99"))
                    lag = "/".join(_fmt(_get(s, f"send_lag_ms.{p}"), 1) for p in ("p95", "max"))
                    md.append(
                        f"| {scenario} | {backend} | {a} | {s['replicate']} | {_fmt(s.get('sent'))} "
                        f"| {_fmt(o.get('ok_200'))} | {_fmt(o.get('queued_202'))} | {_fmt(o.get('rejected_429'))} "
                        f"| {_fmt(o.get('timeout'))} | {_fmt(o.get('transport_error'))} | {_fmt(o.get('http_error'))} "
                        f"| {rtt} | {lag} | {_fmt(_get(s, 'server_decision_us.p95'), 2)} "
                        f"| {_fmt(s.get('processed_events'))} | {_fmt(_mb(_get(s, 'server.rss_bytes_max')))} |"
                    )
        md.append("")

    md += ["## 정합성 검사", ""]
    md += [f"- 문제: {p}" for p in problems] or ["- 모든 회차에서 상태별 합계 = 입력 건수, 재생 1·2회차 판정 일치."]
    md += ["", "## 미실행 (`not_run`)", ""]
    md += [f"- `not_run`: {'/'.join(map(str, p))}" for p in not_run] or ["- 없음 (계획한 조합을 모두 실행)."]
    md += ["", "## 파일", "", "- `comparison.csv`: 회차마다 한 줄, summary.json을 펼친 값",
           "- `<시나리오>/<알고리즘>/<백엔드>/<회차>/`: requests.csv, timeseries.csv, summary.json, environment.json",
           "- 그래프: " + ", ".join(f"`{f.name}`" for f in figures), ""]
    path = exp_dir / "report.md"
    path.write_text("\n".join(md), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: python -m rate_limit_lab.metrics.report <experiment dir>", file=sys.stderr)
        return 2
    path = build_report(Path(args[0]))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
