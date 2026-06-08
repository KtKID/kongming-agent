"""架构合约测试：补 .importlinter 不便实现的检查 + import-linter 合约回归。

B15 / CR 报告 cr-report-20260424-202744.md：
原本只做 symbol 级 ast 扫描（Session / EventSink 不得被 sibling 重定义）；
补充一条 ``test_import_linter_contracts`` 以子进程方式跑 ``lint-imports``，
让 ``.importlinter`` 的四条合约（core-no-sibling-imports /
layered-dependency-direction / tools-no-direct-safety-policy 等）在
``make test-unit`` 路径上被强制回归，不再只依赖 ``make lint`` 手工触发。
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _iter_python_files(pkg: str) -> list[Path]:
    root = REPO_ROOT / pkg
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def test_sessions_do_not_define_session_protocol() -> None:
    """sessions/ 下不允许出现 `class Session(Protocol)` 或 `class Session:` 定义。

    真源是 `core.contracts.Session`，sessions 只能 `from core.contracts import Session`
    或 `from core import Session`，不得重新定义同名协议。
    """
    violations: list[str] = []
    for file in _iter_python_files("sessions"):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "Session":
                violations.append(f"{file.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not violations, (
        "sessions/ must NOT redefine a `Session` class/protocol "
        "(single source of truth is core.contracts.Session). "
        f"Found definitions at: {violations}"
    )


@pytest.mark.parametrize(
    "pkg",
    [
        "tools",
        "sessions",
        "prompting",
        "infrastructure",
        "application",
        "runtime_assembly",
        "hosts",
        "safety",
    ],
)
def test_sibling_packages_do_not_redefine_eventsink(pkg: str) -> None:
    """任何 sibling 包都不许重定义 EventSink Protocol。

    真源是 core.contracts.EventSink；v1-mini "单 Protocol + fan-out 多 sink" 模式。
    """
    violations: list[str] = []
    for file in _iter_python_files(pkg):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "EventSink":
                violations.append(f"{file.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not violations, (
        f"{pkg}/ must NOT redefine an `EventSink` class/protocol. "
        f"Found definitions at: {violations}"
    )


def test_import_linter_contracts() -> None:
    """以子进程方式运行 ``lint-imports``，在 unit 层强制回归 .importlinter 合约。

    必须在 ``make test-unit`` 路径上也能拦截跨层 import 漂移——Makefile 的
    ``make lint`` 是另一条人工 / CI 路径，不该是合约唯一门禁。

    若本机没装 ``lint-imports``（例如开发者只装了 runtime deps），跳过而不是失败。
    """
    if shutil.which("lint-imports") is None:
        pytest.skip("lint-imports CLI not available (requires import-linter)")

    result = subprocess.run(
        ["lint-imports"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        env={**os.environ, "PYTHONUTF8": "1"},
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"import-linter contracts violated. stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
