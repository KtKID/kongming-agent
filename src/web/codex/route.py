"""WebSocket endpoint：``/ws/codex``（v0.1）。

与 :mod:`web.claude_code.route` 平级；通过 spawn ``codex exec --json`` 子进程
驱动，由 :class:`CodexService` 喂出归一化事件流。

入站消息分发（4 类，对应 ccui ``server/openai-codex.js`` 的 ws.onmessage）：

- ``codex-command``         → :meth:`CodexService.query`
- ``abort-session``         → :meth:`CodexService.abort` + send ``complete(aborted=True)``
- ``check-session-status``  → :meth:`SessionManager.is_active` +（active 时）
  :meth:`SessionManager.replace_writer` + send ``session-status``
- 其他                       → ``kind:error`` 帧

鉴权：复用现有 ``src/web/auth.py`` 的 cookie ``kongming_session`` →
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

- 本文件可 import ``web.codex.*`` / ``web._shared.*`` / ``web.auth``
- **不可** import ``web.claude_code.*``（CLAUDE.md 第 11 条：route 不感知厂商内部）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from web._shared.session_manager import SessionManager
from web.auth import SESSION_COOKIE_NAME, verify_session_cookie
from web.codex.service import CodexService
from web.protocol.rest_models import UserInputAttachment

if TYPE_CHECKING:
    from itsdangerous import URLSafeTimedSerializer

logger = logging.getLogger(__name__)

WS_CLOSE_POLICY_VIOLATION = 1008

router = APIRouter()


class WebSocketWriter:
    """duck-typed writer 适配器（``async send_json(msg: dict)``）。

    :class:`CodexService` 的内部 ``_safe_send`` 已经吞了发送异常，本类只是
    把 FastAPI ``WebSocket`` 包一层让接口名字更显式（也方便后续插出站埋点
    或速率限制 hook 时只改这一处）。
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket

    async def send_json(self, msg: dict[str, Any]) -> None:
        await self._ws.send_json(msg)


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
    except Exception as exc:
        logger.exception("codex ws unhandled error")
        with contextlib.suppress(Exception):
            await writer.send_json(
                {
                    "kind": "error",
                    "provider": "codex",
                    "error": f"unhandled: {exc!r}",
                },
            )
        with contextlib.suppress(Exception):
            await websocket.close()


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
    msg_type = data.get("type") if isinstance(data, dict) else None

    if msg_type == "codex-command":
        await _handle_codex_command(
            writer,
            data,
            service,
            bg_tasks,
            thread_id=thread_id,
        )
        return

    if msg_type == "abort-session":
        session_id = data.get("sessionId")
        if not isinstance(session_id, str):
            await _send_error(writer, "abort-session.sessionId required")
            return
        await service.abort(session_id)
        await writer.send_json(
            {
                "kind": "complete",
                "provider": "codex",
                "sessionId": session_id,
                "aborted": True,
            },
        )
        return

    if msg_type == "check-session-status":
        session_id = data.get("sessionId")
        if not isinstance(session_id, str):
            await _send_error(writer, "check-session-status.sessionId required")
            return
        active = sessions.is_active(session_id)
        if active:
            # 重连：把 SessionManager 里旧 writer 换成当前 ws 的 writer
            await sessions.replace_writer(session_id, writer)
        await writer.send_json(
            {
                "type": "session-status",
                "sessionId": session_id,
                "isProcessing": active,
            },
        )
        return

    await _send_error(writer, f"unknown command type: {msg_type!r}")


async def _handle_codex_command(
    writer: WebSocketWriter,
    data: dict[str, Any],
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
    - ``model``           可选 ``-m`` flag
    - ``resume``          True 时必须配真实 ``sessionId``
    - ``sessionId``       resume=True 必填；否则可空，本端生成 ``pending-XXX``
    """
    command = data.get("command", "")
    if not isinstance(command, str):
        await _send_error(writer, "codex-command.command must be string")
        return

    # codex-channel-image-paste §3：解 attachments 字段（前端 CodexView 粘贴图片
    # 后发上来）。容错：
    # - 字段缺失 / None / 非 list / 空 list → attachments=None 走原纯文本路径
    # - 单条 dict 校验失败由 pydantic 抛 ValidationError，捕获后 fallback 到 None
    raw_attachments = data.get("attachments")
    parsed_attachments: list[UserInputAttachment] | None = None
    if isinstance(raw_attachments, list) and raw_attachments:
        try:
            parsed_attachments = [
                UserInputAttachment.model_validate(item) for item in raw_attachments
            ]
        except Exception:
            logger.warning(
                "codex-command attachments parse failed; falling back to text-only",
                exc_info=True,
            )
            parsed_attachments = None

    raw_options = data.get("options")
    options: dict[str, Any] = raw_options if isinstance(raw_options, dict) else {}

    cwd_opt = options.get("cwd")
    cwd = cwd_opt if isinstance(cwd_opt, str) and cwd_opt else _default_cwd()

    permission_mode_opt = options.get("permissionMode", "default")
    permission_mode = permission_mode_opt if isinstance(permission_mode_opt, str) else "default"

    model_opt = options.get("model")
    model = model_opt if isinstance(model_opt, str) and model_opt else None

    resume = bool(options.get("resume"))

    incoming_sid = options.get("sessionId")
    real_sid = incoming_sid if isinstance(incoming_sid, str) and incoming_sid else None

    if resume and real_sid is None:
        await _send_error(writer, "codex-command.resume=true requires sessionId")
        return

    sid = real_sid if real_sid is not None else f"pending-{uuid.uuid4().hex[:8]}"

    # 后台跑——让读循环能继续接 abort-session / check-session-status
    task = asyncio.create_task(
        service.query(
            session_id=sid,
            command=command,
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
    """发送 ``kind:error`` 帧（容错）。"""
    with contextlib.suppress(Exception):
        await writer.send_json(
            {
                "kind": "error",
                "provider": "codex",
                "error": error_message,
            },
        )


__all__ = ["WebSocketWriter", "router"]
