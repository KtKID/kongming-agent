"""unit：ShellTool 基本行为。

用 ``echo`` / ``sleep`` 这种跨平台最通用的命令覆盖：

- stdout 捕获
- 退出码捕获
- ``timeout`` 触发 TimeoutError → 被基类包成结构化失败
- 参数校验失败（空 command / 负 timeout）

Windows 环境下 ``echo`` 是 built-in shell 命令；``sleep`` 未必存在。
这里优先使用 Python 的 ``python -c`` 形式避免平台兼容问题。但 v1-mini
第一版只在 macOS / Linux 场景下跑，这两个命令天然存在。
"""

from __future__ import annotations

import sys

import pytest

from core.contracts import ToolContext
from tools import ShellTool


def _ctx() -> ToolContext:
    return ToolContext(run_id="r", session_id="s", turn=1, call_id="c")


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="shell semantics differ on Windows")
async def test_shell_echo_captures_stdout() -> None:
    tool = ShellTool()
    result = await tool.execute({"command": "echo hello_world"}, _ctx())
    assert result.ok is True
    assert result.data is not None
    assert "hello_world" in result.data["stdout"]
    assert result.data["return_code"] == 0


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="shell semantics differ on Windows")
async def test_shell_nonzero_exit_is_captured() -> None:
    tool = ShellTool()
    # `false` 命令返回 1（POSIX 标准）
    result = await tool.execute({"command": "false"}, _ctx())
    assert result.ok is True  # tool 自身执行成功；command 返回码由 data 承载
    assert result.data is not None
    assert result.data["return_code"] != 0


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="shell semantics differ on Windows")
async def test_shell_timeout_returns_structured_failure() -> None:
    tool = ShellTool()
    # 3 秒 sleep + 0.5 秒超时 → 基类吃掉 TimeoutError 包成 ok=False
    result = await tool.execute(
        {"command": "sleep 3", "timeout": 0.5},
        _ctx(),
    )
    assert result.ok is False
    assert result.error_message is not None
    assert "timed out" in result.error_message.lower()


@pytest.mark.unit
async def test_shell_empty_command_is_rejected() -> None:
    tool = ShellTool()
    result = await tool.execute({"command": "   "}, _ctx())
    assert result.ok is False
    assert "command" in (result.error_message or "").lower()


@pytest.mark.unit
async def test_shell_missing_command_arg_is_rejected() -> None:
    tool = ShellTool()
    result = await tool.execute({}, _ctx())
    assert result.ok is False


@pytest.mark.unit
async def test_shell_invalid_timeout_is_rejected() -> None:
    tool = ShellTool()
    result = await tool.execute({"command": "echo x", "timeout": 0}, _ctx())
    assert result.ok is False
    assert "timeout" in (result.error_message or "").lower()


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="shell semantics differ on Windows")
async def test_shell_cwd_override_uses_provided_directory(tmp_path) -> None:
    tool = ShellTool()
    result = await tool.execute(
        {"command": "pwd", "cwd": str(tmp_path)},
        _ctx(),
    )
    assert result.ok is True
    assert result.data is not None
    # macOS 下 tmp_path 有时会经过 /private 软链；只断言尾段包含一致子串
    assert tmp_path.name in result.data["stdout"]
