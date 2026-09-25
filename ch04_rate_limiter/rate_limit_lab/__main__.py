"""`python -m rate_limit_lab` 진입점.

    python -m rate_limit_lab               # 환경·패키지 정보
    python -m rate_limit_lab rules [PATH]  # 규칙 파일 검증 및 출력
"""

from __future__ import annotations

import argparse
import platform
import sys

from rate_limit_lab import __version__
from rate_limit_lab.config import DEFAULT_RULES_PATH, RuleConfigError, load_rules

SUBPACKAGES = ("algorithms", "api", "load", "metrics", "distributed", "lyft")


def cmd_info(_: argparse.Namespace) -> int:
    import importlib

    print(f"rate_limit_lab {__version__}")
    print(f"python {platform.python_version()} ({sys.executable})")
    print(f"platform {platform.platform()}")
    for name in SUBPACKAGES:
        importlib.import_module(f"rate_limit_lab.{name}")
        print(f"  ok rate_limit_lab.{name}")
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    try:
        rules = load_rules(args.path)
    except RuleConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{args.path}: {len(rules)} rule(s)")
    for rule in rules.values():
        print(
            f"  {rule.rule_id}: scope={rule.scope.value} limit={rule.limit}/{rule.window_ms}ms "
            f"bucket={rule.bucket_capacity} refill={rule.refill_per_sec:g}/s "
            f"queue={rule.queue_capacity} leak={rule.leak_per_sec:g}/s idle_ttl={rule.idle_ttl_ms}ms"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rate_limit_lab")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("info", help="환경·패키지 정보").set_defaults(func=cmd_info)
    rules = sub.add_parser("rules", help="규칙 파일 검증")
    rules.add_argument("path", nargs="?", default=DEFAULT_RULES_PATH)
    rules.set_defaults(func=cmd_rules)
    args = parser.parse_args(argv)
    return args.func(args) if args.command else cmd_info(args)


if __name__ == "__main__":
    sys.exit(main())
