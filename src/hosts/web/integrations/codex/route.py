"""WebSocket endpoint：``/ws/codex``（v0.1）。

与 :mod:`web.integrations.claude_code.route` 平级；通过 spawn ``codex exec --json`` 子进程
驱动，由 :class:`CodexService` 喂出归一化事件流。

入站消息分发（4 类，对应 ccui ``server/openai-codex.js`` 的 ws.onmessage）：

- ``codex-command``         → :meth:`CodexService.query`
- ``abort-session``         → :meth:`CodexService.abort` + send ``complete(aborted=True)``
- ``check-session-status``  → :meth:`SessionManager.is_active` +（active 时）
  :meth:`SessionManager.replace_writer` + send ``session-status``
- 其他                       → ``frame_type:error`` 帧

鉴权：复用现有 ``src/hosts/web/auth/middleware.py`` 的 cookie ``kongming_session`` →
``verify_session_cookie``，与 ``/ws/claude-code`` 同款。

依赖装配（per-connection）：

- ``sessions = ws.app.state.codex_session_manager``（全局单例）
- ``service  = ws.app.state.codex_service``（全局单例，捧着 sessions）

设计要点：

- placeholder session_id：新建 session 时（``resume=False``）若前端没传
  ``sessionId``，本端生成 ``pending-{uuid4hex8}``；service 收到 ``thread.started``
  后由 :meth:`SessionManager.rename` 替换为真实 thread_id
- 断连不 kill 子进程：让 codex 自己跑完写 rollout，用户重连用
  ``check-session-status`` 重新 :meth:`SessionManager.replace_writer`
- WebSocketWriter 适配器：FastAPI ``WebSocket`` 已有 ``async send_json``，duck-typing
  够用，service 接的就是 duck-typed writer；这里再裹一层是为了把出站调用集中、
  也方便后续扩展（如出站埋点）

import 边界：

- 本文件可 import ``web.integrations.codex.*`` / ``web.shared.*`` / ``web.auth``
- **不可** import ``web.integrations.claude_code.*``（CLAUDE.md 第 11 条：route 不感知厂商内部）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from hosts.web.auth.middleware import SESSION_COOKIE_NAME, verify_session_cookie
from hosts.web.integrations.codex.service import CodexService
from hosts.web.protocol import (
    CodexC2SAdapter,
    CodexCommandFrame,
    CodexS2CAdapter,
    SessionStatusFrame,
)
from hosts.web.shared.reconnectable_writer import ReconnectableWebSocketWriter
from hosts.web.shared.session_manager import SessionManager
from network.network_log import log_network_event, log_network_exception

if TYPE_CHECKING:
    from itsdangerous import URLSafeTimedSerializer

logger = logging.getLogger(__name__)

WS_CLOSE_POLICY_VIOLATION = 1008

router = APIRouter()


WebSocketWriter = ReconnectableWebSocketWriter


@router.websocket("/ws/codex")
async def codex_websocket(
    websocket: WebSocket,
    thread_id: str | None = Query(default=None),
) -> None:
    """Codex WebSocket endpoint。

    协议详见 docs/codex-web-integration-v0.1/02-protocol.md。
    """
    # 1. 鉴权（与 /ws/claude-code 同款）
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
        "codex_session_manager",
        None,
    )
    service: CodexService | None = getattr(
        websocket.app.state,
        "codex_service",
        None,
    )
    if sessions is None or service is None:
        await websocket.close(
            code=WS_CLOSE_POLICY_VIOLATION,
            reason="codex_service not configured",
        )
        return

    writer = WebSocketWriter(websocket)
    bg_tasks: set[asyncio.Task[Any]] = set()

    await websocket.accept()

    # 3. 主循环
    try:
        while True:
            data = await websocket.receive_json()
            await _dispatch(
                websocket,
                writer,
                data,
                service,
                sessions,
                bg_tasks,
                thread_id=thread_id,
            )
    except WebSocketDisconnect:
        # 客户端断连：保留活跃 session（不主动 kill 子进程），让用户重连后
        # 通过 check-session-status 重接 writer
        logger.debug("codex ws disconnected")
        log_network_event(
            "hosts.web.integrations.codex.route",
            "ws_disconnected",
            level="INFO",
            message="codex websocket disconnected",
            thread_id=thread_id,
        )
    except Exception as exc:
        logger.exception("codex ws unhandled error")
        try:
            await writer.send_json(
                {
                    "frame_type": "error",
                    "provider": "codex",
                    "error": f"unhandled: {exc!r}",
                },
            )
        except Exception as send_exc:
            log_network_exception(
                "hosts.web.integrations.codex.route",
                "unhandled_error_send_failed",
                send_exc,
                thread_id=thread_id,
            )
        try:
            await websocket.close()
        except Exception as close_exc:
            log_network_exception(
                "hosts.web.integrations.codex.route",
                "unhandled_error_close_failed",
                close_exc,
                thread_id=thread_id,
            )
    finally:
        writer.detach_ws(websocket)


async def _dispatch(
    websocket: WebSocket,
    writer: WebSocketWriter,
    data: dict[str, Any],
    service: CodexService,
    sessions: SessionManager,
    bg_tasks: set[asyncio.Task[Any]],
    *,
    thread_id: str | None,
) -> None:
    """单条入站帧分发。"""
    msg_type = data.get("frame_type") if isinstance(data, dict) else None
    try:
        frame = CodexC2SAdapter.validate_python(data)
    except ValidationError:
        if msg_type == "codex-command" and not isinstance(data.get("command"), str):
            await _send_error(writer, "codex-command.command must be string")
            return
        if msg_type == "abort-session" and not isinstance(data.get("sessionId"), str):
            await _send_error(writer, "abort-session.sessionId required")
            return
        if msg_type == "check-session-status" and not isinstance(data.get("sessionId"), str):
            await _send_error(writer, "check-session-status.sessionId required")
            return
        await _send_error(writer, f"unknown command type: {msg_type!r}")
        return

    if frame.frame_type == "codex-command":
        await _handle_codex_command(
            writer,
            frame,
            service,
            bg_tasks,
            thread_id=thread_id,
        )
        return

    if frame.frame_type == "abort-session":
        session_id = frame.sessionId
        await service.abort(session_id)
        complete_frame = CodexS2CAdapter.validate_python(
            {
                "frame_type": "complete",
                "provider": "codex",
                "sessionId": session_id,
                "aborted": True,
            },
        )
        await writer.send_json(CodexS2CAdapter.dump_python(complete_frame))
        return

    if frame.frame_type == "check-session-status":
        session_id = frame.sessionId
        active = sessions.is_active(session_id)
        if active:
            # 重连：把 SessionManager 里旧 writer 绑定到底层新 ws
            await sessions.replace_writer(session_id, websocket)
        await writer.send_json(
            SessionStatusFrame(
                sessionId=session_id,
                isProcessing=active,
            ).model_dump(),
        )
        return


async def _handle_codex_command(
    writer: WebSocketWriter,
    frame: CodexCommandFrame,
    service: CodexService,
    bg_tasks: set[asyncio.Task[Any]],
    *,
    thread_id: str | None,
) -> None:
    """``codex-command`` 帧 → :meth:`CodexService.query` 后台 task。

    options 解析（与 ccui openai-codex.js 协议对齐）：

    - ``cwd``             默认走 service 自取 ``os.getcwd()``（这里转空串让
                          service 端取默认）—— v0.1 设计：route 不读环境
    - ``permissionMode``  ``default`` / ``acceptEdits`` / ``bypassPermissions``
    - ``model``           可选 ``--model`` flag
    - ``resume``          True 时必须配真实 ``sessionId``
    - ``sessionId``       resume=True 必填；否则可空，本端生成 ``pending-XXX``
    """
    # codex-channel-image-paste：attachments 已在 CodexCommandFrame 校验完成。
    # 字段缺失 / None / 空 list → attachments=None 走纯文本路径。
    parsed_attachments = list(frame.attachments) if frame.attachments else None
    options = frame.options

    cwd = options.cwd if options is not None and options.cwd else _default_cwd()
    permission_mode = (
        options.permissionMode
        if options is not None and options.permissionMode is not None
        else "default"
    )
    model = options.model if options is not None and options.model else None
    resume = bool(options.resume) if options is not None else False
    real_sid = options.sessionId if options is not None and options.sessionId else None

    if resume and real_sid is None:
        await _send_error(writer, "codex-command.resume=true requires sessionId")
        return

    sid = real_sid if real_sid is not None else f"pending-{uuid.uuid4().hex[:8]}"

    # 后台跑——让读循环能继续接 abort-session / check-session-status
    task = asyncio.create_task(
        service.query(
            session_id=sid,
            command=frame.command,
            cwd=cwd,
            permission_mode=permission_mode,
            model=model,
            resume=resume,
            kongming_thread_id=thread_id,
            writer=writer,
            attachments=parsed_attachments,
        ),
    )
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)


def _default_cwd() -> str:
    """缺省 cwd：当前进程工作目录（与 ccui 同款）。"""
    import os

    return os.getcwd()


async def _send_error(writer: WebSocketWriter, error_message: str) -> None:
    """发送 ``frame_type:error`` 帧（容错）。"""
    try:
        await writer.send_json(
            {
                "frame_type": "error",
                "provider": "codex",
                "error": error_message,
            },
        )
    except Exception as exc:
        log_network_exception(
            "hosts.web.integrations.codex.route",
            "send_error_frame_failed",
            exc,
            error_message=error_message,
        )


__all__ = ["WebSocketWriter", "router"]
