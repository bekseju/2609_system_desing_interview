"""HTTP 부하 실험: 서버 N개 기동 → 벽시계 부하 → 결과 저장 → 서버 종료 (TODO 4.4, 4.5).

    # 명세 설정: 예열 10초·측정 60초, 인스턴스 1/2/4, 동시성 1/32/128, 3회 (조합당 70초 이상, 오래 걸림)
    python scripts/run_http_experiment.py --algorithms token_bucket leaking_bucket

    # 축소 설정: 예열 1초·측정 5초, 인스턴스 1/2, 동시성 1/32, 1회, token_bucket·leaking_bucket
    python scripts/run_http_experiment.py --quick

    # 일부만 바꾸기
    python scripts/run_http_experiment.py --instances 1 --concurrency 32 --replicates 1 --measure-s 20

결과: results/<UTC timestamp>/ (또는 --exp-dir로 기존 실험 폴더에 추가)
    <scenario>/<algorithm>/http-i<N>-c<C>/r<rep>/
        requests.csv      요청별 전송·응답·지연·결과 분류
        timeseries.csv    전송 시각 구간별 결과 건수
        summary.json      결과 분류 건수·send lag/rtt/서버 판정 지연·서버 RSS
        environment.json  설정·명령·서버 명령·환경
        server-<i>.log    서버 표준 출력/오류
    experiment.json (plan 포함), comparison.csv, report.md

각 조합·회차는 서버를 새로 띄워 서로 독립이다. 인스턴스가 여러 개면 각자 메모리 상태를 따로 가진다
(공유 저장소 없음 → 같은 사용자의 한도가 인스턴스 수만큼 나뉘어 적용될 수 있다. 5단계에서 다룬다).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rate_limit_lab.algorithms import ALGORITHMS  # noqa: E402
from rate_limit_lab.load.http_load import (  # noqa: E402
    HTTP_TIMESERIES_FIELDS,
    http_timeseries,
    run_load,
    summarize_http,
    write_http_results,
    write_rows,
)
from rate_limit_lab.load.scenarios import generate, load_scenarios, write_generated  # noqa: E402
from rate_limit_lab.metrics.environment import collect_environment, utc_timestamp  # noqa: E402
from rate_limit_lab.metrics.report import build_report  # noqa: E402
from rate_limit_lab.metrics.summary import check_consistency  # noqa: E402
from rate_limit_lab.models import read_requests  # noqa: E402

QUICK = dict(instances=[1, 2], concurrency=[1, 32], replicates=1, warmup_s=1.0, measure_s=5.0,
             algorithms=["token_bucket", "leaking_bucket"])
FULL = dict(instances=[1, 2, 4], concurrency=[1, 32, 128], replicates=3, warmup_s=10.0, measure_s=60.0,
            algorithms=["token_bucket", "leaking_bucket", "fixed_window", "sliding_log", "sliding_counter"])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_healthy(url: str, timeout_s: float = 20) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=1) as response:
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(0.2)
    return False


class Servers:
    """서버 프로세스 N개를 띄우고 정리한다."""

    def __init__(self, algorithm: str, count: int, log_dir: Path, work_ms: int) -> None:
        self.procs, self.urls, self.commands, self.logs = [], [], [], []
        log_dir.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            port = free_port()
            cmd = [sys.executable, "-m", "rate_limit_lab.api.server", "--algorithm", algorithm, "--port", str(port),
                   "--instance-id", f"i{i}", "--work-ms", str(work_ms)]
            log = (log_dir / f"server-{i}.log").open("w", encoding="utf-8")
            self.logs.append(log)
            self.procs.append(subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                               env={**os.environ, "PYTHONIOENCODING": "utf-8"}))
            self.urls.append(f"http://127.0.0.1:{port}")
            self.commands.append(cmd)

    def __enter__(self) -> "Servers":
        for url, proc in zip(self.urls, self.procs):
            if not wait_healthy(url) or proc.poll() is not None:
                self.__exit__(None, None, None)
                raise RuntimeError(f"서버 기동 실패: {url} (로그 확인)")
        return self

    def __exit__(self, *exc) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for log in self.logs:
            log.close()


def run_one(requests, algorithm: str, instances: int, concurrency: int, out: Path, *, warmup_s: float,
            measure_s: float, timeout_s: float, work_ms: int, scenario_meta: dict, replicate: int,
            command: list[str]) -> dict:
    with Servers(algorithm, instances, out, work_ms) as servers:
        data = asyncio.run(run_load(requests, servers.urls, concurrency=concurrency, measure_s=measure_s,
                                    warmup_s=warmup_s, timeout_s=timeout_s))
        commands = servers.commands
    backend = f"http-i{instances}-c{concurrency}"
    summary = {
        "scenario": scenario_meta["scenario"], "algorithm": algorithm, "backend": backend, "replicate": replicate,
        "seed": scenario_meta["seed"], "instances": instances, "concurrency": concurrency,
        "warmup_s": warmup_s, "measure_s": measure_s, "timeout_s": timeout_s, "work_ms": work_ms,
        **summarize_http(data["results"], data["elapsed_s"], data["server_stats"]),
        "warmup": data["warmup"],
        "server_stats": data["server_stats"],
        "units": {
            "send_lag_ms": "ms, 부하 발생기: 실제 전송 - 예정 시각",
            "rtt_ms": "ms, 부하 발생기: 전송 시작 → 응답 수신 완료",
            "server_decision_us": "µs, 서버 내부 decide()/admit() (응답 헤더 X-Decision-Us)",
            "queue_wait_ms": "ms, 서버 단조 시계: 처리 완료 - 접수",
            "server.rss_bytes_*": "bytes, 부하 종료 직후 서버 프로세스 RSS",
        },
    }
    check_consistency(summary)
    write_http_results(out / "requests.csv", data["results"])
    write_rows(out / "timeseries.csv", HTTP_TIMESERIES_FIELDS, http_timeseries(data["results"]))
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    env = collect_environment(command, scenario={k: v for k, v in scenario_meta.items() if k != "rps_bins"},
                              backend=backend, algorithm=algorithm, replicate=replicate, server_commands=commands,
                              clock_source=summary["server"]["clock_source"])
    (out / "environment.json").write_text(json.dumps(env, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quick", action="store_true", help="축소 설정")
    parser.add_argument("--scenario", default="normal")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--algorithms", nargs="+", choices=sorted(ALGORITHMS))
    parser.add_argument("--instances", nargs="+", type=int)
    parser.add_argument("--concurrency", nargs="+", type=int)
    parser.add_argument("--replicates", type=int)
    parser.add_argument("--warmup-s", type=float)
    parser.add_argument("--measure-s", type=float)
    parser.add_argument("--timeout-s", type=float, default=5.0)
    parser.add_argument("--work-ms", type=int, default=0)
    parser.add_argument("--out", type=Path, default=ROOT / "results")
    parser.add_argument("--exp-dir", type=Path, default=None, help="기존 실험 폴더에 결과를 추가")
    args = parser.parse_args(argv)

    base = QUICK if args.quick else FULL
    cfg = {k: getattr(args, k) if getattr(args, k) is not None else v for k, v in base.items()}
    command = [sys.executable, *sys.argv] if argv is None else ["scripts/run_http_experiment.py", *argv]

    exp_dir = args.exp_dir or args.out / utc_timestamp()
    exp_dir.mkdir(parents=True, exist_ok=True)
    input_dir = exp_dir / "inputs" / args.scenario
    if not (input_dir / "requests.csv").exists():
        write_generated(input_dir, generate(load_scenarios()[args.scenario], args.seed))
    meta = json.loads((input_dir / "scenario.json").read_text(encoding="utf-8"))
    requests = read_requests(input_dir / "requests.csv")

    plan = [[args.scenario, a, f"http-i{n}-c{c}", r] for a in cfg["algorithms"] for n in cfg["instances"]
            for c in cfg["concurrency"] for r in range(1, cfg["replicates"] + 1)]
    exp_file = exp_dir / "experiment.json"
    experiment = json.loads(exp_file.read_text(encoding="utf-8")) if exp_file.exists() else {
        "command": command, "started_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": meta["seed"], "plan": []}
    experiment.setdefault("http_runs", []).append({"command": command, "config": cfg, "scenario": args.scenario})
    experiment["plan"] += [p for p in plan if p not in experiment["plan"]]
    exp_file.write_text(json.dumps(experiment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    est = len(plan) * (cfg["warmup_s"] + cfg["measure_s"] + 3)
    print(f"[http] {len(plan)}개 실행, 예상 최소 {est / 60:.1f}분 → {exp_dir}")
    failures = 0
    for scenario, algorithm, backend, rep in plan:
        n, c = (int(x[1:]) for x in backend.split("-")[1:])
        out = exp_dir / scenario / algorithm / backend / f"r{rep}"
        try:
            s = run_one(requests, algorithm, n, c, out, warmup_s=cfg["warmup_s"], measure_s=cfg["measure_s"],
                        timeout_s=args.timeout_s, work_ms=args.work_ms, scenario_meta=meta, replicate=rep,
                        command=command)
        except Exception as exc:  # 한 조합 실패가 전체를 멈추지 않게 기록만 한다 (보고서에 not_run)
            failures += 1
            print(f"[http] 실패 {algorithm}/{backend}/r{rep}: {exc}", file=sys.stderr)
            continue
        o = s["outcomes"]
        print(f"[http] {algorithm}/{backend}/r{rep}: sent={s['sent']} 200={o['ok_200']} 202={o['queued_202']} "
              f"429={o['rejected_429']} timeout={o['timeout']} err={o['transport_error'] + o['http_error']} "
              f"rtt_p95={s['rtt_ms']['p95']:.2f}ms lag_p95={s['send_lag_ms']['p95']:.1f}ms")
    report = build_report(exp_dir)
    print(f"[report] {report}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
