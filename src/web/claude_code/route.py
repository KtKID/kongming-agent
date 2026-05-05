"""WebSocket endpoint：``/ws/claude-code``（v0.1）。

参考 ccui ``server/index.js`` 第 1402-1531 行 ``ws.on('message')`` 路由。

4 类入站命令分发：

- ``claude-command`` → :meth:`ClaudeCodeService.query`
- ``claude-permission-response`` → :meth:`ApprovalBridge.resolve`
- ``abort-session`` → :meth:`ClaudeCodeService.abort` + send ``complete(aborted=True)``
- ``check-session-status`` → :meth:`SessionManager.is_active` +（active 时）
  :meth:`SessionManager.replace_writer` + send 状态

鉴权：复用现有 ``src/web/ws.py`` 模式（cookie ``kongming_session`` →
``verify_session_cookie``）。

v0.1.6 起本 endpoint 接受可选 query 参数 ``thread_id``：当浏览器从一个
``backend_kind="claude_code"`` 的 thread 入口连过来时，会带上 ``thread_id``，
endpoint 校验 thread 存在且 ``backend_kind=="claude_code"``，然后把 thread_id
作为 SessionManager 注册 id（替代原来的 placeholder ``pending-XXX``），让
前端 / 后端在 session id 上一一对应。``thread_id`` 缺省时保持 v0.1 老行为，
让 e2e smoke 与未挂 thread 的调用方仍能跑。

依赖装配（per-connection）：

- ``sessions = ws.app.state.claude_session_manager``（全局单例）
- ``normalizer = ClaudeNormalizer()``
- ``approval = ApprovalBridge(normalizer, sessions)``
- ``service = ClaudeCodeService(normalizer, approval, sessions)``
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from web._shared.session_manager import SessionManager
from web.auth import SESSION_COOKIE_NAME, verify_session_cookie
from web.claude_code.approval import ApprovalBridge
from web.claude_code.normalizer import ClaudeNormalizer
from web.claude_code.service import ClaudeCodeService

if TYPE_CHECKING:
    from itsdangerous import URLSafeTimedSerializer

    from web.types import ThreadManagerProtocol

logger = logging.getLogger(__name__)

WS_CLOSE_POLICY_VIOLATION = 1008

# 与 web.routers.threads.THREAD_ID_RE 同款；本文件不允许 import routers.threads
# （避免反向依赖），直接复制正则字面量。
_THREAD_ID_RE: re.Pattern[str] = re.compile(r"^thread-[a-f0-9]{12}$")

router = APIRouter()


@router.websocket("/ws/claude-code")
async def claude_code_ws(
    websocket: WebSocket,
    thread_id: str | None = Query(default=None),
) -> None:
    """Claude Code WebSocket endpoint。

    Args:
        thread_id: v0.1.6 可选；带上时 endpoint 校验 thread 存在且
            ``backend_kind=="claude_code"``，并以 thread_id 注册 SessionManager。
            不带时保留 v0.1 行为（自管 sessionId / placeholder）。
    """
    # 1. 鉴权（与 /ws/threads 同款 cookie + serializer 验证）
    serializer: URLSafeTimedSerializer | None = getattr(
        websocket.app.state,
        "serializer",
        None,
    )
    if serializer is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="auth not configured")
        return

    raw_cookie = websocket.cookies.get(SESSION_COOKIE_NAME)
    payload = verify_session_cookie(raw_cookie, serializer)
    if payload is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="not authenticated")
        return

    # 2. 装配
    sessions: SessionManager | None = getattr(
        websocket.app.state,
        "claude_session_manager",
        None,
    )
    if sessions is None:
        await websocket.close(
            code=WS_CLOSE_POLICY_VIOLATION,
            reason="claude_session_manager not configured",
        )
        return

    # 2.5 thread_id 校验（可选）。带上时必须满足：格式合法 + thread 存在 + backend_kind=claude_code
    bound_thread_id: str | None = None
    if thread_id is not None:
        if not _THREAD_ID_RE.match(thread_id):
            await websocket.close(
                code=WS_CLOSE_POLICY_VIOLATION,
                reason="invalid thread_id",
            )
            return
        tm: ThreadManagerProtocol | None = getattr(websocket.app.state, "thread_manager", None)
        if tm is None:
            await websocket.close(
                code=WS_CLOSE_POLICY_VIOLATION,
                reason="thread_manager not configured",
            )
            return
        # tm.list_threads 是同步 IO（扫盘）；endpoint 是 async，丢到 to_thread
        metas = await asyncio.to_thread(tm.list_threads)
        meta = next((m for m in metas if m.id == thread_id), None)
        if meta is None:
            await websocket.close(
                code=WS_CLOSE_POLICY_VIOLATION,
                reason="thread not found",
            )
            return
        if getattr(meta, "backend_kind", "generic_chat") != "claude_code":
            await websocket.close(
                code=WS_CLOSE_POLICY_VIOLATION,
                reason="thread backend_kind is not claude_code",
            )
            return
        bound_thread_id = thread_id

    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    # v0.2 透传 thread_manager（可能为 None — 老路径 / 未配置时仍能运行）
    tm_for_service: ThreadManagerProtocol | None = getattr(
        websocket.app.state,
        "thread_manager",
        None,
    )
    service = ClaudeCodeService(
        normalizer,
        approval,
        sessions,
        thread_manager=tm_for_service,
    )

    # 跟踪 fire-and-forget 后台 task，避免 GC 提前回收（asyncio 文档约定）
    bg_tasks: set[asyncio.Task[Any]] = set()

    await websocket.accept()

    # 3. 主循环
    try:
        while True:
            data = await websocket.receive_json()
            await _dispatch(
                websocket,
                data,
                service,
                approval,
                sessions,
                bg_tasks,
                bound_thread_id=bound_thread_id,
            )
    except WebSocketDisconnect:
        # 客户端断连：保留 active sessions 以便重连
        logger.debug("claude-code ws disconnected")
    except Exception as exc:
        logger.exception("claude-code ws unhandled error")
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {
                    "kind": "error",
                    "provider": "claude",
                    "error": f"unhandled: {exc!r}",
                },
            )
        with contextlib.suppress(Exception):
            await websocket.close()


async def _dispatch(
    websocket: WebSocket,
    data: dict[str, Any],
    service: ClaudeCodeService,
    approval: ApprovalBridge,
    sessions: SessionManager,
    bg_tasks: set[asyncio.Task[Any]],
    *,
    bound_thread_id: str | None = None,
) -> None:
    """单条入站帧分发。"""
    msg_type = data.get("type") if isinstance(data, dict) else None

    if msg_type == "claude-command":
        command = data.get("command", "")
        options = data.get("options") or {}
        if not isinstance(command, str):
            await _send_error(websocket, "claude-command.command must be string")
            return
        # 跑成后台 task —— 让读循环能继续接 approval-response / abort
        # 注意：query 内部已 await session_manager.register/unregister，
        # 异常已经在 service.query finally 处理
        # bg_tasks set 持引用避免 GC 提前回收 task（asyncio 约定）
        task = asyncio.create_task(
            service.query(
                command,
                options,
                websocket,
                register_id_override=bound_thread_id,
            ),
        )
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)
        return

    if msg_type == "claude-permission-response":
        request_id = data.get("requestId")
        if not isinstance(request_id, str):
            await _send_error(websocket, "claude-permission-response.requestId required")
            return
        approval.resolve(request_id, data)
        return

    if msg_type == "abort-session":
        session_id = data.get("sessionId")
        if not isinstance(session_id, str):
            await _send_error(websocket, "abort-session.sessionId required")
            return
        await service.abort(session_id)
        await websocket.send_json(
            {
                "kind": "complete",
                "provider": "claude",
                "sessionId": session_id,
                "aborted": True,
            },
        )
        return

    if msg_type == "check-session-status":
        session_id = data.get("sessionId")
        if not isinstance(session_id, str):
            await _send_error(websocket, "check-session-status.sessionId required")
            return
        active = sessions.is_active(session_id)
        if active:
            await sessions.replace_writer(session_id, websocket)
        await websocket.send_json(
            {
                "type": "session-status",
                "sessionId": session_id,
                "isProcessing": active,
            },
        )
        return

    await _send_error(websocket, f"unknown command type: {msg_type!r}")


async def _send_error(websocket: WebSocket, error_message: str) -> None:
    """发送 error 帧。"""
    with contextlib.suppress(Exception):
        await websocket.send_json(
            {
                "kind": "error",
                "provider": "claude",
                "error": error_message,
            },
        )


__all__ = ["router"]
