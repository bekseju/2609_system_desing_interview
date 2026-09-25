"""시나리오 요청 CSV 생성.

    python -m rate_limit_lab.load.generate                       # 모든 시나리오, 기본 seed
    python -m rate_limit_lab.load.generate normal boundary --seed 7
    python -m rate_limit_lab.load.generate --out results/scenarios

결과: <out>/<시나리오>-seed<seed>/requests.csv, scenario.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rate_limit_lab.load.scenarios import DEFAULT_SCENARIOS_PATH, ScenarioConfigError, generate, load_scenarios, write_generated

DEFAULT_OUT = Path("results") / "scenarios"


def scenario_dir(out: Path, name: str, seed: int) -> Path:
    return out / f"{name}-seed{seed}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rate_limit_lab.load.generate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="시나리오 이름 (생략하면 전부)")
    parser.add_argument("--seed", type=int, default=None, help="생략하면 scenarios.yaml의 defaults.seed")
    parser.add_argument("--config", type=Path, default=DEFAULT_SCENARIOS_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    try:
        scenarios = load_scenarios(args.config)
    except ScenarioConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    names = args.names or list(scenarios)
    unknown = [n for n in names if n not in scenarios]
    if unknown:
        print(f"error: 알 수 없는 시나리오 {unknown}. 가능: {list(scenarios)}", file=sys.stderr)
        return 2

    for name in names:
        generated = generate(scenarios[name], args.seed)
        out_dir = scenario_dir(args.out, name, generated.seed)
        meta = write_generated(out_dir, generated)
        print(
            f"{name}: {meta['total_requests']}건 (기대 {meta['expected_total']:g}), "
            f"클라이언트 {meta['unique_clients']}, 평균 {meta['actual_rps_overall']:g} rps "
            f"(목표 {meta['target_rps_overall']:g}) → {out_dir}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
