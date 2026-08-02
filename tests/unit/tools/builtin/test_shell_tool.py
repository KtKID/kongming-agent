"""unit：ShellTool 基本行为。

用 ``echo`` / ``sleep`` 这种跨平台最通用的命令覆盖：

- stdout 捕获
- 退出码捕获
- ``timeout`` 触发 TimeoutError → 被基类包成结构化失败
- 参数校验失败（空 command / 负 timeout）
- **interrupt-run-v0.1**：外部 task.cancel() 时子进程被 kill，PID 收回，
  CancelledError 透传给 runner 顶层

Windows 环境下 ``echo`` 是 built-in shell 命令；``sleep`` 未必存在。
这里优先使用 Python 的 ``python -c`` 形式避免平台兼容问题。但 v1-mini
第一版只在 macOS / Linux 场景下跑，这两个命令天然存在。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from core.contracts import ToolContext
from core.errors import ToolPreparationError
from tests.support.tool_calls import execute_prepared_tool
from tools import ShellTool


def _ctx(cwd: Path | None = None) -> ToolContext:
    metadata = {"cwd": str(cwd or Path.cwd())}
    return ToolContext(run_id="r", session_id="s", turn=1, call_id="c", metadata=metadata)


@pytest.mark.unit
def test_shell_tool_model_descriptions_are_chinese() -> None:
    """模型收到的工具和参数说明均使用中文。"""
    tool = ShellTool()

    assert (
        tool.description
        == "通过 /bin/sh -c（或平台等价实现）执行 shell 命令，并返回标准输出、标准错误和退出码。"
    )
    assert (
        tool.input_schema["properties"]["command"]["description"] == "要执行的 shell 命令字符串。"
    )
    assert (
        tool.input_schema["properties"]["timeout"]["description"]
        == "超时时间，单位为秒；默认值为 30 秒。"
    )
    assert (
        tool.input_schema["properties"]["cwd"]["description"]
        == "命令的工作目录；默认使用调用方的 cwd。"
    )


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="shell semantics differ on Windows")
async def test_shell_echo_captures_stdout() -> None:
    tool = ShellTool()
    result = await execute_prepared_tool(tool, {"command": "echo hello_world"}, _ctx())
    assert result.ok is True
    assert result.data is not None
    assert "hello_world" in result.data["stdout"]
    assert result.data["return_code"] == 0


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="shell semantics differ on Windows")
async def test_shell_nonzero_exit_is_captured() -> None:
    tool = ShellTool()
    # `false` 命令返回 1（POSIX 标准）
    result = await execute_prepared_tool(tool, {"command": "false"}, _ctx())
    assert result.ok is True  # tool 自身执行成功；command 返回码由 data 承载
    assert result.data is not None
    assert result.data["return_code"] != 0


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="shell semantics differ on Windows")
async def test_shell_timeout_returns_structured_failure() -> None:
    tool = ShellTool()
    # 3 秒 sleep + 0.5 秒超时 → 基类吃掉 TimeoutError 包成 ok=False
    result = await execute_prepared_tool(
        tool,
        {"command": "sleep 3", "timeout": 0.5},
        _ctx(),
    )
    assert result.ok is False
    assert result.error_message is not None
    assert "timed out" in result.error_message.lower()


@pytest.mark.unit
async def test_shell_empty_command_is_rejected() -> None:
    tool = ShellTool()
    result = await execute_prepared_tool(tool, {"command": "   "}, _ctx())
    assert result.ok is False
    assert "command" in (result.error_message or "").lower()


@pytest.mark.unit
async def test_shell_missing_command_arg_is_rejected() -> None:
    tool = ShellTool()
    result = await execute_prepared_tool(tool, {}, _ctx())
    assert result.ok is False


@pytest.mark.unit
async def test_shell_invalid_timeout_is_rejected() -> None:
    tool = ShellTool()
    result = await execute_prepared_tool(tool, {"command": "echo x", "timeout": 0}, _ctx())
    assert result.ok is False
    assert "timeout" in (result.error_message or "").lower()


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="shell semantics differ on Windows")
async def test_shell_cwd_override_uses_provided_directory(tmp_path) -> None:
    tool = ShellTool()
    result = await execute_prepared_tool(
        tool,
        {"command": "pwd", "cwd": str(tmp_path)},
        _ctx(),
    )
    assert result.ok is True
    assert result.data is not None
    # macOS 下 tmp_path 有时会经过 /private 软链；只断言尾段包含一致子串
    assert tmp_path.name in result.data["stdout"]


@pytest.mark.unit
async def test_shell_default_cwd_uses_context_cwd(tmp_path: Path) -> None:
    tool = ShellTool()
    command = f'"{sys.executable}" -c "import os; print(os.getcwd())"'

    result = await execute_prepared_tool(tool, {"command": command}, _ctx(tmp_path))

    assert result.ok is True
    assert result.data is not None
    assert Path(result.data["stdout"].strip()).resolve() == tmp_path.resolve()
    assert Path(result.data["cwd"]).resolve() == tmp_path.resolve()


@pytest.mark.unit
async def test_shell_relative_cwd_resolves_against_context_cwd(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    tool = ShellTool()
    command = f'"{sys.executable}" -c "import os; print(os.getcwd())"'

    result = await execute_prepared_tool(tool, {"command": command, "cwd": "child"}, _ctx(tmp_path))

    assert result.ok is True
    assert result.data is not None
    assert Path(result.data["stdout"].strip()).resolve() == child.resolve()
    assert Path(result.data["cwd"]).resolve() == child.resolve()


@pytest.mark.unit
def test_shell_prepare_freezes_canonical_cwd_and_is_idempotent(tmp_path: Path) -> None:
    """relative cwd、父目录与软链接最终都冻结为同一真实目录。"""
    target = tmp_path / "target"
    target.mkdir()
    nested = target / "nested"
    nested.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    tool = ShellTool()
    context = _ctx(tmp_path)

    prepared = tool.prepare({"command": "pwd", "cwd": "link/nested/.."}, context)
    repeated = tool.prepare(dict(prepared.arguments), context)

    assert prepared.execution_scope.cwd == target.resolve().as_posix()
    assert prepared.arguments["cwd"] == target.resolve().as_posix()
    assert repeated == prepared


@pytest.mark.unit
def test_shell_prepare_preserves_canonical_absolute_cwd(tmp_path: Path) -> None:
    """绝对 cwd 直接规范化，并保持模型输入映射不变。"""
    tool = ShellTool()
    arguments = {"command": "pwd", "cwd": tmp_path.as_posix()}

    prepared = tool.prepare(arguments, _ctx(tmp_path))

    assert prepared.arguments == {
        "command": "pwd",
        "cwd": tmp_path.resolve().as_posix(),
        "timeout": 30.0,
    }
    assert prepared.execution_scope.cwd == tmp_path.resolve().as_posix()
    assert arguments == {"command": "pwd", "cwd": tmp_path.as_posix()}


@pytest.mark.unit
def test_shell_prepare_rejects_missing_context_cwd() -> None:
    """完整缺失 cwd 时 preparation 失败关闭。"""
    tool = ShellTool()
    context = ToolContext(run_id="r", session_id="s", turn=1, call_id="c")

    with pytest.raises(ToolPreparationError, match="requires ToolContext") as exc_info:
        tool.prepare({"command": "pwd"}, context)
    assert exc_info.value.details["code"] == "cwd_unavailable"


@pytest.mark.unit
def test_shell_prepare_rejects_missing_and_non_directory_cwd(tmp_path: Path) -> None:
    """不存在路径和文件路径都在审批前失败。"""
    tool = ShellTool()
    context = _ctx(tmp_path)
    plain_file = tmp_path / "file.txt"
    plain_file.write_text("x", encoding="utf-8")

    with pytest.raises(ToolPreparationError, match="cwd not found") as missing:
        tool.prepare({"command": "pwd", "cwd": "missing"}, context)
    assert missing.value.details["code"] == "cwd_not_found"
    with pytest.raises(ToolPreparationError, match="not a directory") as non_directory:
        tool.prepare({"command": "pwd", "cwd": str(plain_file)}, context)
    assert non_directory.value.details["code"] == "cwd_not_directory"


# ---------------------------------------------------------------------------
# interrupt-run-v0.1：外部 cancel 时子进程 PID 必须收回
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` 不抛 = 进程存在；ESRCH = 已死。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only PID check")
async def test_shell_cancel_kills_subprocess_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """interrupt-run-v0.1：外部 ``task.cancel()`` 时

    1. 子进程被 ``process.kill()`` + ``await wait()``，PID 收回（无孤儿）
    2. ``CancelledError`` 必须透传给 runner 顶层，**不**被 ``BaseBuiltinTool.execute``
       的 ``except Exception`` 吞掉（Python 3.8+ ``CancelledError`` 不继承
       ``Exception``，但加这条单测钉住不许有人改成 ``except BaseException``）
    """
    tool = ShellTool()
    captured: dict[str, Any] = {}

    # patch 注入 hook 让我们拿到 process 对象（用来抓 PID）
    real_create = asyncio.create_subprocess_shell

    async def _capturing_create(*args: Any, **kwargs: Any) -> Any:
        proc = await real_create(*args, **kwargs)
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(
        "tools.builtin.shell_tool.asyncio.create_subprocess_shell",
        _capturing_create,
    )

    # 跑一个不会自然退出的 sleep；timeout 给大值，确保不被超时机制兜底
    task = asyncio.create_task(
        execute_prepared_tool(tool, {"command": "sleep 60", "timeout": 120.0}, _ctx())
    )

    # 等子进程起来（最多等 1s）
    for _ in range(50):
        if "proc" in captured and captured["proc"].pid:
            break
        await asyncio.sleep(0.02)
    assert "proc" in captured, "subprocess did not start in time"
    pid = int(captured["proc"].pid)
    assert _pid_alive(pid), f"PID {pid} not alive right after spawn"

    # 外部 cancel
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 给 OS 一点时间回收 PID（POSIX SIGKILL 通常 <50ms）
    for _ in range(50):
        if not _pid_alive(pid):
            break
        await asyncio.sleep(0.02)

    assert not _pid_alive(pid), (
        f"PID {pid} still alive after cancel — interrupt-run-v0.1 兜底失效，留孤儿进程"
    )
