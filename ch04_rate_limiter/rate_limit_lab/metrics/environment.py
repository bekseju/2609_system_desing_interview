"""실행 환경 기록 (environment.json)."""

from __future__ import annotations

import os
import platform
import sys
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import psutil

from rate_limit_lab import __version__

PACKAGES = ("PyYAML", "psutil", "matplotlib", "aiohttp", "numpy")


def utc_timestamp() -> str:
    """폴더 이름에 쓰는 UTC 시각: 20260926T013000Z"""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    if sys.platform.startswith("linux"):
        try:
            for line in open("/proc/cpuinfo", encoding="utf-8"):
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def _package_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in PACKAGES:
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def collect_environment(command: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "recorded_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "command": command if command is not None else [sys.executable, *sys.argv],
        "cwd": os.getcwd(),
        "rate_limit_lab_version": __version__,
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation(),
                   "executable": sys.executable},
        "os": {"system": platform.system(), "release": platform.release(), "version": platform.version(),
               "platform": platform.platform()},
        "machine": {"arch": platform.machine(), "cpu": _cpu_name(), "logical_cpus": os.cpu_count(),
                    "physical_cpus": psutil.cpu_count(logical=False),
                    "total_memory_bytes": psutil.virtual_memory().total},
        "packages": _package_versions(),
        **extra,
    }
