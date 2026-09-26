"""로컬 실험 한 번에 실행: 요청 생성 → 알고리즘별 재생·측정 → 보고서 (TODO 3.7).

    python scripts/run_experiment.py                 # 전체: 시나리오 5 × 알고리즘 6 × 3회
    python scripts/run_experiment.py --quick         # 축소: boundary, hot_key, normal × 6 × 1회
    python scripts/run_experiment.py --scenarios boundary --algorithms fixed_window sliding_log --replicates 2

결과: results/<UTC timestamp>/
    experiment.json                      실행 명령·인수·계획(plan)·입력 해시
    inputs/<scenario>/requests.csv, scenario.json
    <scenario>/<algorithm>/memory/r<N>/   requests.csv, timeseries.csv, summary.json, environment.json
    comparison.csv, report.md, *.png

재현: 같은 --seed와 시나리오 설정이면 inputs의 CSV 해시가 같고 판정·건수도 같다.
지연·메모리 값은 장비와 부하에 따라 달라진다.
각 회차는 새 Python 프로세스에서 실행한다 (RSS peak가 프로세스 단위이기 때문).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rate_limit_lab.algorithms import ALGORITHMS  # noqa: E402
from rate_limit_lab.load.scenarios import generate, load_scenarios, write_generated  # noqa: E402
from rate_limit_lab.metrics.environment import utc_timestamp  # noqa: E402
from rate_limit_lab.metrics.report import ALGORITHM_ORDER, build_report  # noqa: E402

QUICK_SCENARIOS = ["boundary", "hot_key", "normal"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenarios", nargs="+", default=None)
    parser.add_argument("--algorithms", nargs="+", choices=sorted(ALGORITHMS), default=None)
    parser.add_argument("--replicates", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--out", type=Path, default=ROOT / "results")
    parser.add_argument("--quick", action="store_true", help="축소 설정 (시나리오 3개, 1회)")
    args = parser.parse_args(argv)

    scenarios_cfg = load_scenarios()
    scenarios = args.scenarios or (QUICK_SCENARIOS if args.quick else list(scenarios_cfg))
    algorithms = args.algorithms or ALGORITHM_ORDER
    replicates = args.replicates or (1 if args.quick else 3)
    unknown = [s for s in scenarios if s not in scenarios_cfg]
    if unknown:
        parser.error(f"알 수 없는 시나리오 {unknown}")

    exp_dir = args.out / utc_timestamp()
    exp_dir.mkdir(parents=True, exist_ok=False)
    plan = [[s, a, "memory", r] for s in scenarios for a in algorithms for r in range(1, replicates + 1)]
    experiment = {
        "command": [sys.executable, *sys.argv] if argv is None else ["scripts/run_experiment.py", *argv],
        "started_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": args.seed if args.seed is not None else "scenarios.yaml defaults.seed",
        "scenarios": scenarios, "algorithms": algorithms, "replicates": replicates,
        "plan": plan, "inputs": {},
    }

    # 1) 요청 생성
    for name in scenarios:
        generated = generate(scenarios_cfg[name], args.seed)
        meta = write_generated(exp_dir / "inputs" / name, generated)
        experiment["seed"] = generated.seed
        experiment["inputs"][name] = {"requests": meta["total_requests"], "sha256": meta["requests_csv_sha256"]}
        print(f"[generate] {name}: {meta['total_requests']}건 sha256={meta['requests_csv_sha256'][:12]}")
    (exp_dir / "experiment.json").write_text(json.dumps(experiment, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")

    # 2) 회차별 재생·측정 (각각 새 프로세스)
    failures = 0
    started = time.perf_counter()
    for scenario, algorithm, backend, rep in plan:
        out = exp_dir / scenario / algorithm / backend / f"r{rep}"
        cmd = [sys.executable, "-m", "rate_limit_lab.metrics.run_local", "--scenario-dir",
               str(exp_dir / "inputs" / scenario), "--algorithm", algorithm, "--out", str(out),
               "--replicate", str(rep)]
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
        if result.returncode != 0:
            failures += 1
            print(f"[replay] 실패 {scenario}/{algorithm}/r{rep}:\n{result.stderr}", file=sys.stderr)
        else:
            print(f"[replay] {result.stdout.strip()}")

    # 3) 보고서
    report = build_report(exp_dir)
    experiment["finished_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    experiment["elapsed_s"] = round(time.perf_counter() - started, 1)
    experiment["failures"] = failures
    (exp_dir / "experiment.json").write_text(json.dumps(experiment, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
    print(f"[report] {report}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
