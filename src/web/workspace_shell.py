"""Workspace shell 运行时。"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import struct
import termios
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from web.claude_code.jsonl_history import jsonl_path_for

AnsiCallback = Callable[[str], Awaitable[None]]
StatusCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _normalize_terminal_text(text: str) -> str:
    """把 PTY 输出压成当前面板更容易消费的纯文本。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\x1b":
            if i + 1 < len(text) and text[i + 1] == "[":
                i += 2
                while i < len(text) and not text[i].isalpha():
                    i += 1
                i += 1
                continue
            i += 1
            continue
        if ch == "\x08":
            if out:
                out.pop()
            i += 1
            continue
        if ch >= " " or ch in "\n\t":
            out.append(ch)
        i += 1
    return "".join(out)


def build_claude_command(*, sdk_session_id: str) -> list[str]:
    """生成 Claude shell 启动命令。"""
    command = ["claude"]
    if sdk_session_id.strip():
        command.extend(["--resume", sdk_session_id.strip()])
    return command


def build_system_shell_command() -> list[str]:
    """生成当前环境的 plain shell 启动命令。"""
    shell = os.environ.get("SHELL", "/bin/zsh").strip() or "/bin/zsh"
    return [shell, "-l"]


def is_claude_command(command: list[str]) -> bool:
    """判断命令是否为 Claude CLI。"""
    return bool(command) and Path(command[0]).name == "claude"


def claude_project_dir_for(
    cwd: str | Path,
    *,
    claude_home: Path | None = None,
) -> Path:
    """根据 workspace cwd 计算 `~/.claude/projects/<encoded-cwd>/` 目录。"""
    return jsonl_path_for(str(cwd), "__probe__", claude_home=claude_home).parent


def list_claude_session_ids(
    cwd: str | Path,
    *,
    claude_home: Path | None = None,
) -> set[str]:
    """列出当前 workspace 已存在的 Claude session ids。"""
    project_dir = claude_project_dir_for(cwd, claude_home=claude_home)
    if not project_dir.is_dir():
        return set()
    return {path.stem for path in project_dir.glob("*.jsonl") if path.is_file()}


def _is_confirmed_claude_session_file(
    path: Path,
    *,
    expected_session_id: str,
    cwd: str | Path | None = None,
) -> bool:
    """确认候选 jsonl 文件确实属于一条新 Claude session。"""
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") != "system" or entry.get("subtype") != "init":
                    continue
                if entry.get("sessionId") != expected_session_id:
                    return False
                entry_cwd = entry.get("cwd")
                if cwd is not None and isinstance(entry_cwd, str) and entry_cwd != str(cwd):
                    return False
                return True
    except OSError:
        return False
    return False


async def wait_for_new_claude_session(
    cwd: str | Path,
    *,
    known_session_ids: set[str],
    claude_home: Path | None = None,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.5,
) -> str | None:
    """等待当前 workspace 里出现新的 Claude session 文件。"""
    project_dir = claude_project_dir_for(cwd, claude_home=claude_home)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if project_dir.is_dir():
            candidates = [
                path
                for path in project_dir.glob("*.jsonl")
                if path.is_file() and path.stem not in known_session_ids
            ]
            if candidates:
                latest = max(candidates, key=lambda path: path.stat().st_mtime_ns)
                if _is_confirmed_claude_session_file(
                    latest,
                    expected_session_id=latest.stem,
                    cwd=cwd,
                ):
                    return latest.stem
        await asyncio.sleep(poll_interval_seconds)
    return None


class WorkspaceShellProcess:
    """单连接 shell 进程封装。"""

    def __init__(
        self,
        *,
        command: list[str],
        cwd: Path,
        emit_output: AnsiCallback,
        emit_status: StatusCallback,
        create_subprocess_exec: Any = None,
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._emit_output = emit_output
        self._emit_status = emit_status
        self._create_subprocess_exec = (
            create_subprocess_exec
            if create_subprocess_exec is not None
            else asyncio.create_subprocess_exec
        )
        self._process: asyncio.subprocess.Process | None = None
        self._master_fd: int | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None

    async def start(self, *, cols: int = 120, rows: int = 32) -> None:
        """启动 PTY + 子进程。"""
        if self._process is not None:
            return
        master_fd, slave_fd = os.openpty()
        self._master_fd = master_fd
        self.resize(cols=cols, rows=rows)
        env = os.environ.copy()
        env.setdefault("TERM", "dumb")
        env.setdefault("NO_COLOR", "1")
        env.setdefault("CLAUDE_CODE_SIMPLE", "1")
        try:
            process = await self._create_subprocess_exec(
                *self._command,
                cwd=str(self._cwd),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                start_new_session=True,
            )
        finally:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        self._process = process
        await self._emit_status(
            {
                "type": "shell-status",
                "status": "running",
                "cwd": str(self._cwd),
                "command": self._command,
            }
        )
        self._reader_task = asyncio.create_task(self._pump_output())
        self._wait_task = asyncio.create_task(self._wait_for_exit())

    async def write(self, data: str) -> None:
        """写入用户输入。"""
        if self._master_fd is None:
            return
        os.write(self._master_fd, data.encode("utf-8"))

    def resize(self, *, cols: int, rows: int) -> None:
        """调整 PTY 尺寸。"""
        if self._master_fd is None:
            return
        winsize = struct.pack("HHHH", max(rows, 1), max(cols, 20), 0, 0)
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)

    async def terminate(self) -> None:
        """终止子进程并释放 FD。"""
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        if self._wait_task is not None:
            with contextlib.suppress(Exception):
                await self._wait_task
        await self._close_fds()

    async def _pump_output(self) -> None:
        if self._master_fd is None:
            return
        loop = asyncio.get_running_loop()
        while True:
            try:
                chunk = await loop.run_in_executor(None, os.read, self._master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text = _normalize_terminal_text(chunk.decode("utf-8", errors="replace"))
            if text:
                await self._emit_output(text)

    async def _wait_for_exit(self) -> None:
        process = self._process
        if process is None:
            return
        return_code = await process.wait()
        await self._emit_status(
            {
                "type": "shell-status",
                "status": "exited",
                "cwd": str(self._cwd),
                "command": self._command,
                "exitCode": return_code,
            }
        )
        await self._close_fds()

    async def _close_fds(self) -> None:
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None
