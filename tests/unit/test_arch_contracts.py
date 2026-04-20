"""架构合约测试：补 .importlinter 不便实现的检查。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _iter_python_files(pkg: str) -> list[Path]:
    root = REPO_ROOT / pkg
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def test_context_does_not_define_session_protocol() -> None:
    """context/ 下不允许出现 `class Session(Protocol)` 或 `class Session:` 定义。

    真源是 `core.contracts.Session`，context 只能 `from core.contracts import Session`
    或 `from core import Session`，不得重新定义同名协议。
    """
    violations: list[str] = []
    for file in _iter_python_files("context"):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "Session":
                violations.append(f"{file.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not violations, (
        "context/ must NOT redefine a `Session` class/protocol "
        "(single source of truth is core.contracts.Session). "
        f"Found definitions at: {violations}"
    )


@pytest.mark.parametrize(
    "pkg",
    ["tools", "context", "executors", "observability", "host", "cli", "safety", "config_loader"],
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
