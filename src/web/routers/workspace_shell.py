"""Workspace shell WebSocket。"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from web.auth import SESSION_COOKIE_NAME, verify_session_cookie
from web.workspace import WorkspaceError, get_thread_meta, require_workspace_root
from web.workspace_shell import (
    WorkspaceShellProcess,
    build_claude_command,
    build_system_shell_command,
    is_claude_command,
    list_claude_session_ids,
    wait_for_new_claude_session,
)

if TYPE_CHECKING:
    from itsdangerous import URLSafeTimedSerializer

    from web.types import ThreadManagerProtocol

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
    if meta is None or meta.sdk_session_id.strip():
        return
    with contextlib.suppress(Exception):
        await tm.bind_sdk_session(thread_id, new_session_id, str(cwd))


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
        with contextlib.suppress(Exception):
            await websocket.close()
        return

    command = (
        build_claude_command(sdk_session_id=meta.sdk_session_id)
        if meta.backend_kind == "claude_code"
        else build_system_shell_command()
    )
    emit_output = lambda text: send_frame({"type": "shell-output", "data": text})
    claude_home = getattr(websocket.app.state, "claude_home", None)
    bind_task: asyncio.Task[None] | None = None
    process: WorkspaceShellProcess | None = None
    known_session_ids = (
        list_claude_session_ids(root, claude_home=claude_home)
        if meta.backend_kind == "claude_code"
        and not meta.sdk_session_id.strip()
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
            and not meta.sdk_session_id.strip()
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
            with contextlib.suppress(Exception):
                if process is not None:
                    await process.terminate()
            try:
                process = make_process(fallback_command)
                await send_starting_status(fallback_command)
                await process.start()
            except Exception as fallback_exc:
                await send_frame({"type": "shell-error", "detail": str(fallback_exc)})
                with contextlib.suppress(Exception):
                    await websocket.close()
                return
        else:
            with contextlib.suppress(Exception):
                if process is not None:
                    await process.terminate()
            await send_frame({"type": "shell-error", "detail": str(exc)})
            with contextlib.suppress(Exception):
                await websocket.close()
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
        pass
    finally:
        if bind_task is not None:
            bind_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await bind_task
        with contextlib.suppress(Exception):
            await process.terminate()
