"""最小 shell builtin tool。

:class:`ShellTool` 提供一个 "运行一条 shell 命令，拿回 stdout/stderr/exit code"
的最小工具。使用 :func:`asyncio.create_subprocess_shell` 实现，不会阻塞
async 主链路。

安全边界（重要）：

- 本模块**不**维护任何 command 黑名单 / 白名单。命令是否允许执行，全部
  由上层 :mod:`safety.capability_policy` + :mod:`safety.permission_policy` +
  :class:`core.contracts.ApprovalProvider` 串联决定。
- 本模块**不 import** ``safety/`` 下任何内部 policy 组件（硬约束，
  import-linter 会红）。
- tool 层的设计原则是"能做的事尽量做对"，安全判断交给 safety 层。

运行策略：

- 默认超时 30 秒，可由参数 ``timeout`` 覆盖。超时后先 ``terminate``，仍不退出
  再 ``kill``，并抛 :class:`TimeoutError`（被基类包成结构化失败）。
- stdout 和 stderr 各自最多保留 8000 字节，超过时截断并标注 ``truncated=True``。
- 默认工作目录来自当前进程；参数 ``cwd`` 可覆盖。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core.contracts import Tool, ToolContext
from tools.runtime.base import BaseBuiltinTool

# 默认参数统一收到这里，便于测试/调试时一眼看清。
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_STREAM_BYTES = 8000
_TERMINATE_GRACE_SECONDS = 2.0


class ShellTool(BaseBuiltinTool):
    """在当前机器上执行一条 shell 命令。

    参数：

    - ``command`` (必填)：要执行的 shell 命令字符串，原样交给 ``/bin/sh -c``
      （或平台等价物）。
    - ``timeout`` (可选)：秒，默认取构造参数 ``default_timeout``；超时后 kill 并返回失败。
    - ``cwd`` (可选)：工作目录；不存在时抛错。

    返回 ``content`` 是一段简短的文本概览（方便模型阅读），``data`` 结构化字段：

    - ``stdout``: str（可能被截断）
    - ``stderr``: str（可能被截断）
    - ``return_code``: int
    - ``truncated``: bool
    - ``command``: 原始 command 字符串
    - ``cwd``: 实际使用的工作目录；未指定时为 ``None``
    """

    name = "run_shell"
    description = (
        "Execute a shell command via /bin/sh -c (or platform equivalent) "
        "and return stdout/stderr/exit code."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command string to execute.",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds. Defaults to 30.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for the command. Defaults to the caller's cwd.",
            },
        },
        "required": ["command"],
    }

    def __init__(
        self,
        *,
        default_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        max_stream_bytes: int = _MAX_STREAM_BYTES,
        terminate_grace_seconds: float = _TERMINATE_GRACE_SECONDS,
    ) -> None:
        self._default_timeout = default_timeout
        self._max_stream_bytes = max_stream_bytes
        self._terminate_grace_seconds = terminate_grace_seconds

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        command = args["command"]
        if not isinstance(command, str) or not command.strip():
            raise ValueError("'command' must be a non-empty string")

        timeout = args.get("timeout", self._default_timeout)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("'timeout' must be a positive number")

        raw_cwd = args.get("cwd")
        cwd_str: str | None = None
        if raw_cwd is not None:
            if not isinstance(raw_cwd, str) or not raw_cwd:
                raise ValueError("'cwd' must be a non-empty string if provided")
            cwd_path = Path(raw_cwd).expanduser().resolve()
            if not cwd_path.exists():
                raise FileNotFoundError(f"cwd not found: {cwd_path}")
            if not cwd_path.is_dir():
                raise NotADirectoryError(f"cwd is not a directory: {cwd_path}")
            cwd_str = str(cwd_path)

        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd_str,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except TimeoutError as exc:
            # 尽量优雅地收尾：先 terminate 给几秒缓冲，不行再 kill。
            if process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(
                        process.wait(),
                        timeout=self._terminate_grace_seconds,
                    )
                except TimeoutError:
                    process.kill()
                    try:
                        await process.wait()
                    except Exception:
                        pass
                except ProcessLookupError:
                    pass
            raise TimeoutError(
                f"shell command timed out after {timeout} seconds: {command!r}"
            ) from exc
        except asyncio.CancelledError:
            # interrupt-run-v0.1：外部 task.cancel()（用户 interrupt）。
            # ``communicate()`` 抛 CancelledError 后子进程仍在跑，pipe 句柄要
            # 等 process 对象 GC 才释放 —— 这会留孤儿进程（rm -rf / 长 build
            # 类命令尤其危险）。这里强制 kill + wait 把 PID 收回，再 raise
            # 让 runner._execute_tool_calls 的 except 接住做占位。
            #
            # 不走 terminate（SIGTERM）/ grace 等待：interrupt 是用户主动且
            # 即时的操作，越快收回越好；terminate 等同于 timeout 的 "再给两秒"
            # 友好策略，cancel 路径不需要。
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    # 进程刚好自然退出，无所谓
                    pass
                try:
                    await process.wait()
                except Exception:
                    # process.wait 失败无所谓，主目标是把 CancelledError 透传
                    pass
            raise

        stdout_text, stdout_truncated = _clip(stdout_bytes, self._max_stream_bytes)
        stderr_text, stderr_truncated = _clip(stderr_bytes, self._max_stream_bytes)
        truncated = stdout_truncated or stderr_truncated
        return_code = int(process.returncode or 0)

        data: dict[str, Any] = {
            "stdout": stdout_text,
            "stderr": stderr_text,
            "return_code": return_code,
            "truncated": truncated,
            "command": command,
            "cwd": cwd_str,
        }

        summary_lines = [f"exit={return_code}"]
        if stdout_text:
            summary_lines.append(f"stdout:\n{stdout_text}")
        if stderr_text:
            summary_lines.append(f"stderr:\n{stderr_text}")
        if truncated:
            summary_lines.append(f"[truncated to {self._max_stream_bytes} bytes per stream]")
        return "\n".join(summary_lines), data


def _clip(raw: bytes, max_bytes: int = _MAX_STREAM_BYTES) -> tuple[str, bool]:
    """把一段 bytes 截断到 max_bytes，返回 (文本, 是否截断)。"""
    truncated = len(raw) > max_bytes
    payload = raw[:max_bytes] if truncated else raw
    return payload.decode("utf-8", errors="replace"), truncated


def build_shell_tool(
    enabled: bool = True,
    *,
    default_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_stream_bytes: int = _MAX_STREAM_BYTES,
    terminate_grace_seconds: float = _TERMINATE_GRACE_SECONDS,
) -> list[Tool]:
    """构造 v1-mini 第一版 shell 工具集。

    Args:
        enabled: ``False`` 时返回空列表，对应 ``config.tool.shell.enabled = false``。
        default_timeout: 命令默认超时秒数。
        max_stream_bytes: stdout/stderr 各自最多保留的字节数。
        terminate_grace_seconds: 超时后 terminate 到 kill 之间的等待秒数。

    Returns:
        只包含一个 :class:`ShellTool` 实例的列表；保留列表形状是为了后续
        扩展（例如补一个 ``BashOutputTool`` / ``BashKillTool`` 之类）时
        调用方签名不用改。
    """
    if not enabled:
        return []
    return [
        ShellTool(
            default_timeout=default_timeout,
            max_stream_bytes=max_stream_bytes,
            terminate_grace_seconds=terminate_grace_seconds,
        )
    ]


__all__ = ["ShellTool", "build_shell_tool"]
