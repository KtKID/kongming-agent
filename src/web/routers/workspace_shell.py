"""Workspace shell WebSocket。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections.abc import Awaitable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from network.network_log import log_network_event, log_network_exception
from web.auth import SESSION_COOKIE_NAME, verify_session_cookie
from web.integrations.claude_code.jsonl_history import jsonl_path_for
from web.workspace.model import WorkspaceError, get_thread_meta, require_workspace_root

try:
    from web.workspace.shell import (
        WorkspaceShellProcess,
        build_claude_command,
        build_system_shell_command,
        is_claude_command,
        list_claude_session_ids,
        wait_for_new_claude_session,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"fcntl", "termios"}:
        raise

    def build_claude_command(*, claude_thread_id: str) -> list[str]:
        command = ["claude"]
        if claude_thread_id.strip():
            command.extend(["--resume", claude_thread_id.strip()])
        return command

    def build_system_shell_command() -> list[str]:
        shell = os.environ.get("SHELL", "/bin/zsh").strip() or "/bin/zsh"
        return [shell, "-l"]

    def is_claude_command(command: list[str]) -> bool:
        return bool(command) and Path(command[0]).name == "claude"

    def list_claude_session_ids(
        cwd: str | Path,
        *,
        claude_home: Path | None = None,
    ) -> set[str]:
        project_dir = jsonl_path_for(str(cwd), "__probe__", claude_home=claude_home).parent
        if not project_dir.is_dir():
            return set()
        return {path.stem for path in project_dir.glob("*.jsonl") if path.is_file()}

    async def wait_for_new_claude_session(
        cwd: str | Path,
        *,
        known_session_ids: set[str],
        claude_home: Path | None = None,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.5,
    ) -> str | None:
        del timeout_seconds, poll_interval_seconds
        new_ids = list_claude_session_ids(cwd, claude_home=claude_home) - known_session_ids
        return sorted(new_ids)[-1] if new_ids else None

    class WorkspaceShellProcess:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise RuntimeError("workspace shell runtime unavailable on this platform")


if TYPE_CHECKING:
    from itsdangerous import URLSafeTimedSerializer

    from web.threads.types import ThreadManagerProtocol

router = APIRouter()

_THREAD_ID_RE: re.Pattern[str] = re.compile(r"^thread-[a-f0-9]{12}$")
WS_CLOSE_POLICY_VIOLATION = 1008


async def _bind_new_claude_session_when_detected(
    *,
    tm: ThreadManagerProtocol,
    thread_id: str,
    cwd: Path,
    claude_home: Path | None,
    known_session_ids: set[str],
) -> None:
    """等待新 session 文件出现后，把它绑定回当前 thread。"""
    new_session_id = await wait_for_new_claude_session(
        cwd,
        known_session_ids=known_session_ids,
        claude_home=claude_home,
    )
    if not new_session_id:
        return
    metas = await asyncio.to_thread(tm.list_threads)
    meta = get_thread_meta(thread_id, metas)
    if meta is None or meta.claude_thread_id.strip():
        return
    try:
        await tm.bind_claude_thread(thread_id, new_session_id, str(cwd))
    except Exception as exc:
        log_network_exception(
            "web.routers.workspace_shell",
            "bind_claude_thread_failed",
            exc,
            thread_id=thread_id,
            session_id=new_session_id,
        )


@router.websocket("/ws/workspace-shell")
async def workspace_shell_ws(
    websocket: WebSocket,
    thread_id: str = Query(...),
) -> None:
    """按 thread 绑定 workspace shell。"""
    serializer: URLSafeTimedSerializer | None = getattr(
        websocket.app.state,
        "serializer",
        None,
    )
    if serializer is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="auth not configured")
        return
    payload = verify_session_cookie(websocket.cookies.get(SESSION_COOKIE_NAME), serializer)
    if payload is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="not authenticated")
        return
    if not _THREAD_ID_RE.match(thread_id):
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="invalid thread_id")
        return

    tm: ThreadManagerProtocol | None = getattr(websocket.app.state, "thread_manager", None)
    if tm is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="thread_manager missing")
        return
    metas = await asyncio.to_thread(tm.list_threads)
    meta = get_thread_meta(thread_id, metas)
    if meta is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="thread not found")
        return

    await websocket.accept()
    send_lock = asyncio.Lock()

    async def send_frame(frame: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json(frame)

    try:
        root = require_workspace_root(meta)
    except WorkspaceError as exc:
        await send_frame({"type": "shell-error", "detail": str(exc)})
        try:
            await websocket.close()
        except Exception as close_exc:
            log_network_exception(
                "web.routers.workspace_shell",
                "close_after_workspace_error_failed",
                close_exc,
                thread_id=thread_id,
            )
        return

    command = (
        build_claude_command(claude_thread_id=meta.claude_thread_id)
        if meta.backend_kind == "claude_code"
        else build_system_shell_command()
    )

    def emit_output(text: str) -> Awaitable[None]:
        return send_frame({"type": "shell-output", "data": text})

    claude_home = getattr(websocket.app.state, "claude_home", None)
    bind_task: asyncio.Task[None] | None = None
    process: WorkspaceShellProcess | None = None
    known_session_ids = (
        list_claude_session_ids(root, claude_home=claude_home)
        if meta.backend_kind == "claude_code"
        and not meta.claude_thread_id.strip()
        and is_claude_command(command)
        else set()
    )

    async def send_starting_status(command_to_run: list[str]) -> None:
        await send_frame(
            {
                "type": "shell-status",
                "status": "starting",
                "cwd": str(root),
                "command": command_to_run,
            }
        )

    def make_process(command_to_run: list[str]) -> WorkspaceShellProcess:
        return WorkspaceShellProcess(
            command=command_to_run,
            cwd=root,
            emit_output=emit_output,
            emit_status=send_frame,
        )

    try:
        process = make_process(command)
        await send_starting_status(command)
        await process.start()
        if (
            meta.backend_kind == "claude_code"
            and not meta.claude_thread_id.strip()
            and is_claude_command(command)
        ):
            bind_task = asyncio.create_task(
                _bind_new_claude_session_when_detected(
                    tm=tm,
                    thread_id=thread_id,
                    cwd=root,
                    claude_home=claude_home,
                    known_session_ids=known_session_ids,
                )
            )
    except Exception as exc:
        fallback_command = build_system_shell_command()
        if meta.backend_kind == "claude_code":
            await send_frame(
                {
                    "type": "shell-error",
                    "detail": f"{exc}; fallback to workspace shell",
                }
            )
            try:
                if process is not None:
                    await process.terminate()
            except Exception as term_exc:
                log_network_exception(
                    "web.routers.workspace_shell",
                    "terminate_before_fallback_failed",
                    term_exc,
                    thread_id=thread_id,
                )
            try:
                process = make_process(fallback_command)
                await send_starting_status(fallback_command)
                await process.start()
            except Exception as fallback_exc:
                await send_frame({"type": "shell-error", "detail": str(fallback_exc)})
                try:
                    await websocket.close()
                except Exception as close_exc:
                    log_network_exception(
                        "web.routers.workspace_shell",
                        "close_after_fallback_failed",
                        close_exc,
                        thread_id=thread_id,
                    )
                return
        else:
            try:
                if process is not None:
                    await process.terminate()
            except Exception as term_exc:
                log_network_exception(
                    "web.routers.workspace_shell",
                    "terminate_after_spawn_failed",
                    term_exc,
                    thread_id=thread_id,
                )
            await send_frame({"type": "shell-error", "detail": str(exc)})
            try:
                await websocket.close()
            except Exception as close_exc:
                log_network_exception(
                    "web.routers.workspace_shell",
                    "close_after_spawn_failed",
                    close_exc,
                    thread_id=thread_id,
                )
            return

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type") if isinstance(data, dict) else None
            if msg_type == "shell-input":
                payload = data.get("data", "")
                if isinstance(payload, str):
                    await process.write(payload)
                continue
            if msg_type == "shell-resize":
                cols = int(data.get("cols", 120) or 120)
                rows = int(data.get("rows", 32) or 32)
                process.resize(cols=cols, rows=rows)
                continue
            if msg_type == "shell-terminate":
                await process.terminate()
                await send_frame(
                    {
                        "type": "shell-status",
                        "status": "terminated",
                        "cwd": str(root),
                        "command": command,
                    }
                )
                continue
            await send_frame(
                {
                    "type": "shell-error",
                    "detail": f"unknown shell frame type: {msg_type!r}",
                }
            )
    except WebSocketDisconnect:
        log_network_event(
            "web.routers.workspace_shell",
            "ws_disconnected",
            level="INFO",
            message="workspace shell websocket disconnected",
            thread_id=thread_id,
        )
    finally:
        if bind_task is not None:
            bind_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await bind_task
        try:
            await process.terminate()
        except Exception as exc:
            log_network_exception(
                "web.routers.workspace_shell",
                "final_terminate_failed",
                exc,
                thread_id=thread_id,
            )
