"""0.1 프로젝트 구조와 `python -m` 실행."""

import importlib

import pytest

from rate_limit_lab.__main__ import SUBPACKAGES
from tests.conftest import run_python


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_importable(name):
    importlib.import_module(f"rate_limit_lab.{name}")


@pytest.mark.parametrize("dirname", ["tests", "scripts", "configs", "docs"])
def test_root_directories_exist(project_root, dirname):
    assert (project_root / dirname).is_dir()


def test_python_m_runs_from_root():
    result = run_python("-m", "rate_limit_lab")
    assert result.returncode == 0, result.stderr
    for name in SUBPACKAGES:
        assert f"ok rate_limit_lab.{name}" in result.stdout


def test_python_m_rules_validates_default():
    result = run_python("-m", "rate_limit_lab", "rules")
    assert result.returncode == 0, result.stderr
    assert "user_default: scope=user limit=10/1000ms" in result.stdout
