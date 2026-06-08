"""Import 边界单元测试（dev-checklist 8.8）。

通过 subprocess 调用 ``lint-imports`` CLI 验证 Contract 11 / Contract 12
仍 Kept。CLI exit 0 = 全 contract pass；非 0 = 任意 contract broken。

为什么走 subprocess 而不是 import-linter Python API？

- import-linter 的 Python API ``importlinter.cli.lint_imports`` 在不同
  版本签名不一致，单测耦合 API 版本会脆；CLI 接口稳定。
- pre-push hook 跑的就是 CLI，单测和 CI 行为一致。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _resolve_lint_imports_cmd() -> str:
    """优先用同 venv 下的 ``lint-imports`` 脚本，找不到时跳过测试。

    pre-commit / CI 必装 import-linter 包；本地极个别情况（没装 dev 依赖）
    才会缺失，跳过比挂掉对开发更友好。
    """
    cmd = shutil.which("lint-imports")
    if cmd is None:
        pytest.skip("lint-imports CLI not available; install import-linter")
    return cmd


def _run_lint_imports() -> subprocess.CompletedProcess[str]:
    """跑 lint-imports CLI，返回 CompletedProcess 供调用方断言。"""
    cmd = _resolve_lint_imports_cmd()
    return subprocess.run(
        [cmd, "--config", str(_REPO_ROOT / ".importlinter")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_network_self_contained_contract_kept() -> None:
    """Contract 11: network 包不依赖业务层 ``web``。"""
    result = _run_lint_imports()
    assert "network must not depend on business modules" in result.stdout, result.stdout
    # KEPT 大写是 import-linter 默认输出
    assert "network must not depend on business modules KEPT" in result.stdout, (
        f"Contract 11 broken; stdout=\n{result.stdout}"
    )


def test_network_tools_private_contract_kept() -> None:
    """Contract 12: network.tools 仅 network 包内部可见。"""
    result = _run_lint_imports()
    assert "network.tools is private to network package" in result.stdout, result.stdout
    assert "network.tools is private to network package KEPT" in result.stdout, (
        f"Contract 12 broken; stdout=\n{result.stdout}"
    )


def test_lint_imports_exit_code_zero() -> None:
    """所有 Contract 全过 → CLI exit 0。"""
    result = _run_lint_imports()
    assert result.returncode == 0, (
        f"lint-imports exit {result.returncode}\nstdout=\n{result.stdout}\nstderr=\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# fix-network-log-misplacement: 旧路径运行期不可 import（负向验证）
#
# `observability.network_log` 与 `web.integrations.claude_code.keepalive_log` 已搬到
# `network/` 包。这两条旧 import path 必须从运行期消失（不留 deprecation
# shim），任何漏改的调用方应在 import 阶段立即 raise ModuleNotFoundError。
# ---------------------------------------------------------------------------


def test_legacy_observability_network_log_path_removed() -> None:
    """旧路径 ``observability.network_log`` 必须 raise ModuleNotFoundError。

    搬迁后 ``network_log`` 唯一真源 = ``network.network_log``。
    """
    with pytest.raises(ModuleNotFoundError):
        __import__("observability.network_log")


def test_legacy_claude_code_keepalive_log_path_removed() -> None:
    """旧路径 ``web.integrations.claude_code.keepalive_log`` 必须 raise ModuleNotFoundError。

    搬迁后 ``keepalive_log`` 唯一真源 = ``network.keepalive_log``。
    """
    with pytest.raises(ModuleNotFoundError):
        __import__("web.integrations.claude_code.keepalive_log")
