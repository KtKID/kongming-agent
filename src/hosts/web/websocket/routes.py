"""WebSocket 端点（v0.1.5）。

路径：``/ws/threads/{thread_id}``

流程（建连）：

1. cookie 验证（``kongming_session``）→ 失败 ``ws.close(1008)``
2. ``thread_id`` 正则校验 → 失败 ``ws.close(1008)``
3. ``thread_manager.boot_or_attach(thread_id)`` → metadata 不存在 → ``ws.close(1008)``
4. ``ws.accept()`` + ``cell.attach_ws(ws)``
5. 推 ``thread.history`` 帧（v0.1.5 简化：从 cell.runtime 取 session 历史）
6. 入帧循环：

   - ``user.input``  → ``asyncio.create_task(cell.bridge.run_once(text))``
   - ``approval.ack`` → ``cell.adapter.resolve_approval(call_id, approved)``
   - ``ping``         → 回 ``pong``

   - 帧 > 1MB → 推 ``error`` 帧 + 继续读
   - JSON 解析失败 / 字段不匹配 → 推 ``error`` 帧 + 继续读
   - 客户端断连 → ``cell.adapter`` / ws_event_sink 静默吞 send 失败

设计要点：

- ``run_once`` 用 :func:`asyncio.create_task` 不阻塞读循环 —— 推理过程中
  ``approval.ack`` 才能送达；任务出错由 done_callback 推 ``error`` 帧。
- protocol-frame-type-unify-v0.2：discriminated union 字段 ``kind`` →
  ``frame_type``；``_dispatch_frame`` 内分派也从 ``frame.kind`` 切到
  ``frame.frame_type``。
- 1MB 限制：``len(raw_text)`` 字节而非字符数（中文 UTF-8 约 3 字节 / 字）。
- ``cell.runtime`` 的 session history 由 ``runtime._sessions[thread_id]`` 提供；
  v0.1.5 通过 ``runtime.session_factory`` 兜底拿（需要 :class:`NativeRuntime` 暴露此接口）。
  如果 runtime 接口不直接暴露 history，``thread.history`` 帧降级为空消息列表。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from evolution.state_store import EvolutionStateStore
from evolution.store import EvolutionStore, resolve_evolution_root
from hosts.web.app_support.llm_protocol import NormalizedMessage
from hosts.web.approvals.auto.ws_handlers import (
    handle_auto_approval_query,
    handle_auto_approval_toggle,
)
from hosts.web.auth.middleware import SESSION_COOKIE_NAME, verify_session_cookie
from hosts.web.generic_channel_log import (
    log_generic_channel_event,
    log_generic_channel_exception,
)
from hosts.web.protocol import (
    ErrorFrame,
    PongFrame,
    SystemNoticeFrame,
    ThreadHistoryFrame,
    WSFrameC2SAdapter,
)
from network import get_network_manager
from network.network_log import log_network_event, log_network_exception

if TYPE_CHECKING:
    from itsdangerous import URLSafeTimedSerializer

    from hosts.web.threads.types import ThreadManagerProtocol

logger = logging.getLogger(__name__)

THREAD_ID_RE: re.Pattern[str] = re.compile(r"^thread-[a-f0-9]{12}$")
MAX_FRAME_BYTES = 1_000_000  # 1 MB
"""单帧最大字节数（UTF-8 编码后）。"""

WS_CLOSE_POLICY_VIOLATION = 1008


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def register_ws_routes(app: FastAPI) -> None:
    """把 WS 端点注册到 FastAPI app（由 :func:`web.app.create_app` 调）。"""

    @app.websocket("/ws/threads/{thread_id}")
    async def thread_ws(websocket: WebSocket, thread_id: str) -> None:
        await _thread_ws_handler(websocket, thread_id)


async def _thread_ws_handler(websocket: WebSocket, thread_id: str) -> None:
    """WS 连接生命周期：鉴权 → boot → 推 history → 入帧循环 → cleanup。"""
    # 1. cookie 验
    serializer: URLSafeTimedSerializer | None = getattr(websocket.app.state, "serializer", None)
    if serializer is None:
        log_generic_channel_event(
            "auth_rejected",
            level="WARNING",
            thread_id=thread_id,
            reason="auth_not_configured",
        )
        # 装配缺失，按 1008 关
        await websocket.close(
            code=WS_CLOSE_POLICY_VIOLATION,
            reason="auth not configured",
        )
        return

    raw_cookie = websocket.cookies.get(SESSION_COOKIE_NAME)
    payload = verify_session_cookie(raw_cookie, serializer)
    if payload is None:
        log_generic_channel_event(
            "auth_rejected",
            level="WARNING",
            thread_id=thread_id,
            reason="not_authenticated",
        )
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="not authenticated")
        return

    # 2. thread_id 正则
    if not THREAD_ID_RE.match(thread_id):
        log_generic_channel_event(
            "thread_rejected",
            level="WARNING",
            thread_id=thread_id,
            reason="invalid_thread_id",
        )
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="invalid thread id")
        return

    # 3. boot_or_attach
    tm: ThreadManagerProtocol = websocket.app.state.thread_manager
    try:
        cell = await tm.boot_or_attach(thread_id)
    except KeyError:
        log_generic_channel_event(
            "thread_rejected",
            level="WARNING",
            thread_id=thread_id,
            reason="thread_not_found",
        )
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="thread not found")
        return
    except Exception as exc:
        logger.exception("boot_or_attach failed for thread_id=%s", thread_id)
        log_generic_channel_exception(
            "boot_failed",
            exc,
            level="ERROR",
            thread_id=thread_id,
        )
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="boot failed")
        return

    # 4. accept
    await websocket.accept()
    network_manager = getattr(websocket.app.state, "network_manager", None)
    if network_manager is None:
        network_manager = get_network_manager()
    conn_id = await network_manager.register("generic", websocket, thread_id)
    log_generic_channel_event("registered", thread_id=thread_id, conn_id=conn_id)
    cell.attach_ws(websocket)
    cell.touch()

    # 5. 推 thread.history
    try:
        await _send_history_frame(websocket, cell)
        await _send_evolution_replay_frames(websocket, cell)
        log_generic_channel_event("history_replay_sent", thread_id=thread_id, conn_id=conn_id)
    except Exception as exc:
        logger.exception("send history frame failed for thread_id=%s", thread_id)
        log_generic_channel_exception(
            "history_replay_failed",
            exc,
            thread_id=thread_id,
            conn_id=conn_id,
        )
        # 不关连接，只是 history 失败 —— 让用户继续对话

    # 6. 入帧循环
    try:
        await _receive_loop(websocket, cell, tm, thread_id, network_manager, conn_id)
    finally:
        with contextlib.suppress(Exception):
            await network_manager.unregister(conn_id)
        log_generic_channel_event("unregistered", thread_id=thread_id, conn_id=conn_id)
        # cleanup：只注销当前 ws；cell / adapter / runtime 生命周期继续由
        # ThreadManager 管，避免同一 thread 的其它连接被一条断连带走。
        detach_call = getattr(cell, "detach_ws", None)
        if callable(detach_call):
            try:
                detach_call(websocket)
            except Exception as exc:
                log_network_exception(
                    "hosts.web.websocket.routes",
                    "detach_ws_failed",
                    exc,
                    thread_id=thread_id,
                )
                log_generic_channel_exception(
                    "detach_ws_failed",
                    exc,
                    thread_id=thread_id,
                    conn_id=conn_id,
                )


async def _receive_loop(
    websocket: WebSocket,
    cell: Any,
    tm: ThreadManagerProtocol,
    thread_id: str,
    network_manager: Any,
    conn_id: str,
) -> None:
    """C2S 帧分发循环；客户端断连 → :class:`WebSocketDisconnect` → 退出。"""
    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            log_network_event(
                "hosts.web.websocket.routes",
                "ws_disconnected",
                level="INFO",
                message="generic chat websocket disconnected",
                thread_id=thread_id,
            )
            log_generic_channel_event("disconnected", thread_id=thread_id, conn_id=conn_id)
            return
        except Exception as exc:
            logger.exception("ws receive_text raised; closing")
            log_generic_channel_exception(
                "receive_failed",
                exc,
                level="ERROR",
                thread_id=thread_id,
                conn_id=conn_id,
            )
            return

        # 1MB 限制（按 UTF-8 字节）
        raw_bytes = len(raw.encode("utf-8"))
        if raw_bytes > MAX_FRAME_BYTES:
            log_generic_channel_event(
                "frame_rejected",
                level="WARNING",
                thread_id=thread_id,
                conn_id=conn_id,
                reason="frame_too_large",
                raw_bytes=raw_bytes,
            )
            await _send_error_frame(websocket, "internal", "frame too large (>1MB)")
            continue

        try:
            data = json.loads(raw)
        except Exception as exc:
            log_generic_channel_exception(
                "frame_parse_failed",
                exc,
                level="WARNING",
                thread_id=thread_id,
                conn_id=conn_id,
                raw_bytes=raw_bytes,
            )
            await _send_error_frame(websocket, "internal", f"frame parse error: {exc}")
            continue

        # smart-approval v0.5：auto-approval-toggle / auto-approval-query 是
        # 命令式帧，**不在** WSFrameC2S discriminated union 内（union 只收
        # user.input / approval.ack / ping / interrupt 四种流式帧）—— 旁路
        # validate_json，直接拿原 dict 派发到共用 handler。这样既避免污染
        # 协议（命令式语义跟流式分派不混），也避免 ValidationError 把
        # auto-approval 帧识别成"非法帧"。其他帧仍按原 union 校验。
        #
        # 字段命名：peek 读 ``frame_type``（protocol-frame-type-unify-v0.2
        # 之后 wire 协议从历史的 ``kind`` / ``type`` 统一到 ``frame_type``；
        # 前端 useAutoApproval.ts 和 claude_code route 都已切，本旁路漏改过
        # 一次导致整帧被 union 拒，现修正）。
        peek_type: str | None = None
        if isinstance(data, dict):
            t = data.get("frame_type")
            if isinstance(t, str):
                peek_type = t
        if peek_type in ("auto-approval-toggle", "auto-approval-query"):
            log_generic_channel_event(
                "auto_approval_frame",
                thread_id=thread_id,
                conn_id=conn_id,
                frame_type=peek_type,
                raw_bytes=raw_bytes,
                cwd_present=bool(data.get("cwd")) if isinstance(data, dict) else None,
            )
            cell.touch()
            policy = getattr(websocket.app.state, "auto_approval_policy", None)
            if peek_type == "auto-approval-toggle":
                await handle_auto_approval_toggle(
                    websocket,
                    data,
                    policy,
                    channel="generic_chat",
                )
            else:
                await handle_auto_approval_query(
                    websocket,
                    data,
                    policy,
                    channel="generic_chat",
                )
            continue

        if isinstance(data, dict) and await network_manager.handle_inbound(conn_id, data):
            log_generic_channel_event(
                "heartbeat_frame_consumed",
                thread_id=thread_id,
                conn_id=conn_id,
                frame_type=peek_type,
                raw_bytes=raw_bytes,
                client_ts=data.get("ts"),
            )
            continue

        # JSON 解析 + discriminated union
        try:
            frame = WSFrameC2SAdapter.validate_python(data)
        except ValidationError as exc:
            log_generic_channel_event(
                "frame_validation_failed",
                level="WARNING",
                thread_id=thread_id,
                conn_id=conn_id,
                frame_type=peek_type,
                raw_bytes=raw_bytes,
                error_count=len(exc.errors()),
            )
            await _send_error_frame(websocket, "internal", f"invalid frame: {exc.errors()[:3]}")
            continue
        except Exception as exc:
            log_generic_channel_exception(
                "frame_validation_failed",
                exc,
                level="WARNING",
                thread_id=thread_id,
                conn_id=conn_id,
                frame_type=peek_type,
                raw_bytes=raw_bytes,
            )
            await _send_error_frame(websocket, "internal", f"frame parse error: {exc}")
            continue

        cell.touch()
        log_generic_channel_event(
            "frame_dispatch",
            thread_id=thread_id,
            conn_id=conn_id,
            frame_type=frame.frame_type,
            raw_bytes=raw_bytes,
            **_frame_log_fields(frame),
        )
        await _dispatch_frame(frame, cell, websocket, tm, thread_id, conn_id)


async def _dispatch_frame(
    frame: Any,
    cell: Any,
    websocket: WebSocket,
    tm: ThreadManagerProtocol,
    thread_id: str,
    conn_id: str,
) -> None:
    """分派单个 C2S 帧。"""
    frame_type = frame.frame_type
    if frame_type == "user.input":
        # 后台跑 run_once；不阻塞读循环
        effort = getattr(frame, "reasoning_effort", None)
        # claude-image-paste-e2e #20：把 UserInputAttachment(BaseModel) 列表
        # 提前打成 dict，全链路（runtime / runner / Message.metadata / assembler
        # / provider）保持 dict 形态，避免下游再处理 BaseModel ↔ dict 双形态。
        raw_attachments = getattr(frame, "attachments", None)
        attachments_dicts: list[dict[str, Any]] | None = (
            [a.model_dump() for a in raw_attachments] if raw_attachments else None
        )
        task = asyncio.create_task(
            _run_once_safely(
                cell,
                frame.text,
                websocket,
                reasoning_effort=effort,
                attachments=attachments_dicts,
            ),
            name=f"web-run-once-{thread_id}",
        )
        # 把 task 暂存到 cell（便于 evict 时 cancel）
        cell.current_run_task = task

        # interrupt-run-v0.1：done_callback 在 task 完成时（正常 / 异常 / cancel）
        # 把 cell.current_run_task 清成 None，避免下次收到 InterruptFrame 时
        # 看到一个已 done 的 task 误判为"正在跑"。callback 只在 task 仍是当前
        # 引用时才清，防止 race（新 run 刚启动覆盖了 current_run_task）。
        def _clear_run_task(
            t: asyncio.Task[Any], *, _cell: Any = cell, _task: asyncio.Task[Any] = task
        ) -> None:
            if getattr(_cell, "current_run_task", None) is _task:
                _cell.current_run_task = None

        task.add_done_callback(_clear_run_task)
        log_generic_channel_event(
            "run_task_started",
            thread_id=thread_id,
            conn_id=conn_id,
            frame_type=frame_type,
            request_id=getattr(frame, "request_id", None),
            text_len=len(frame.text),
            reasoning_effort=effort,
            attachment_count=len(attachments_dicts) if attachments_dicts else 0,
        )
    elif frame_type == "interrupt":
        # interrupt-run-v0.1：浏览器点 Stop。检查当前 run 是否真在跑：
        # - None / 已 done：推 system notice "no_active_run"，不 cancel
        # - 否则 task.cancel() → runner 顶层 except → emit run.cancelled
        #   → WSEventSink fanout 转 RunInterruptedFrame（多 tab 自动同步）
        current_task: asyncio.Task[Any] | None = getattr(cell, "current_run_task", None)
        if current_task is None or current_task.done():
            await _send_no_active_run_notice(websocket, thread_id)
            log_generic_channel_event(
                "interrupt_no_active_run",
                thread_id=thread_id,
                conn_id=conn_id,
                frame_type=frame_type,
                run_id=getattr(frame, "run_id", None),
            )
        else:
            current_task.cancel()
            logger.info(
                "interrupt requested for thread=%s; cancelled current_run_task",
                thread_id,
            )
            log_generic_channel_event(
                "interrupt_requested",
                thread_id=thread_id,
                conn_id=conn_id,
                frame_type=frame_type,
                run_id=getattr(frame, "run_id", None),
            )
    elif frame_type == "approval.ack":
        # v0.1.6 三态：传递字符串字面值给 thread_manager，由它转 ApprovalAction
        # 枚举（thread_manager 在装配层，可 import core.contracts；ws 是 app shell
        # 层不允许）。非法字段降级为 REJECT 由 thread_manager 处理。
        try:
            tm.resolve_approval(thread_id, frame.call_id, frame.action)
            log_generic_channel_event(
                "approval_ack_resolved",
                thread_id=thread_id,
                conn_id=conn_id,
                frame_type=frame_type,
                call_id=frame.call_id,
                action=frame.action,
            )
        except Exception as exc:
            logger.exception("resolve_approval raised; ignored")
            log_generic_channel_exception(
                "approval_ack_resolve_failed",
                exc,
                thread_id=thread_id,
                conn_id=conn_id,
                frame_type=frame_type,
                call_id=frame.call_id,
                action=frame.action,
            )
    elif frame_type == "ping":
        try:
            pong = PongFrame(timestamp_ms=_now_ms(), ts=getattr(frame, "ts", None))
            await websocket.send_json(pong.model_dump())
            log_generic_channel_event(
                "legacy_ping_pong_sent",
                thread_id=thread_id,
                conn_id=conn_id,
                frame_type=frame_type,
                client_ts=getattr(frame, "ts", None),
            )
        except Exception as exc:
            # 推 pong 失败说明 ws 断了；让下次 receive 抛 WebSocketDisconnect
            log_network_exception(
                "hosts.web.websocket.routes",
                "pong_send_failed",
                exc,
                thread_id=thread_id,
            )
            log_generic_channel_exception(
                "legacy_ping_pong_failed",
                exc,
                thread_id=thread_id,
                conn_id=conn_id,
                frame_type=frame_type,
                client_ts=getattr(frame, "ts", None),
            )
    else:
        # discriminated union 已经过滤；这里只是兜底
        await _send_error_frame(websocket, "internal", f"unknown frame_type: {frame_type}")


def _frame_log_fields(frame: Any) -> dict[str, Any]:
    """Return safe, content-free fields for generic channel diagnostics."""

    frame_type = getattr(frame, "frame_type", None)
    if frame_type == "user.input":
        attachments = getattr(frame, "attachments", None)
        return {
            "request_id": getattr(frame, "request_id", None),
            "text_len": len(getattr(frame, "text", "") or ""),
            "reasoning_effort": getattr(frame, "reasoning_effort", None),
            "attachment_count": len(attachments) if attachments else 0,
        }
    if frame_type == "approval.ack":
        return {
            "call_id": getattr(frame, "call_id", None),
            "action": getattr(frame, "action", None),
        }
    if frame_type == "interrupt":
        return {"run_id": getattr(frame, "run_id", None)}
    if frame_type == "ping":
        return {"client_ts": getattr(frame, "ts", None)}
    return {}


async def _run_once_safely(
    cell: Any,
    text: str,
    websocket: WebSocket,
    *,
    reasoning_effort: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    """在后台跑 ``cell.bridge.run_once``；异常推 ``error`` 帧不沉默死掉。

    token 持久化由 :class:`UsagePersistSink` 在每个 turn 的 ``usage``
    event 时增量写盘，不在此处做 run 结束一次性写入。

    ``attachments`` 是 :class:`web.protocol.rest_models.UserInputAttachment`
    经 ``model_dump()`` 后的 dict 列表；为 None 表示纯文本输入。一路透传到
    :meth:`core.runner.Runner._seed_messages`，最终写入 user
    :class:`core.message.Message` 的 ``metadata["attachments"]``。
    """
    try:
        await cell.bridge.run_once(
            text,
            reasoning_effort=reasoning_effort,
            attachments=attachments,
        )
        log_generic_channel_event(
            "run_once_completed",
            thread_id=getattr(cell, "thread_id", None),
            text_len=len(text),
            reasoning_effort=reasoning_effort,
            attachment_count=len(attachments) if attachments else 0,
        )
    except asyncio.CancelledError:
        log_generic_channel_event(
            "run_once_cancelled",
            thread_id=getattr(cell, "thread_id", None),
            text_len=len(text),
            reasoning_effort=reasoning_effort,
            attachment_count=len(attachments) if attachments else 0,
        )
        raise
    except Exception as exc:
        logger.exception("run_once failed; emitting error frame")
        log_generic_channel_exception(
            "run_once_failed",
            exc,
            level="ERROR",
            thread_id=getattr(cell, "thread_id", None),
            text_len=len(text),
            reasoning_effort=reasoning_effort,
            attachment_count=len(attachments) if attachments else 0,
        )
        with contextlib.suppress(Exception):
            await _send_error_frame(
                websocket,
                "llm_error",
                f"run failed: {type(exc).__name__}: {exc}",
            )


async def _send_history_frame(websocket: WebSocket, cell: Any) -> None:
    """从 ``cell.runtime`` 取 session history 包成 :class:`ThreadHistoryFrame` 推。

    实现细节：

    - 优先走 ``runtime._session_factory(thread_id)`` 创建 Session →
      ``await session.history()``。对 FileSession / SQLiteSession 后端，
      新建 session 仍可从磁盘读取已持久化的历史。
    - 降级：若 factory 返回空，再查 ``runtime._sessions[thread_id]``（内存缓存）。
    - history 转换：runtime 的 message dict（``role`` + ``content``）→
      :class:`web.app_support.llm_protocol.NormalizedMessage`，与 Claude/Codex history
      形态对齐。
    """
    messages: list[NormalizedMessage] = []
    runtime = getattr(cell, "runtime", None)
    thread_id: str = cell.thread_id

    history: list[Any] = []
    if runtime is not None:
        # 优先：runtime._session_factory(thread_id) → Session 对象 →
        # await session.history()（history 是 async 方法）
        sf = getattr(runtime, "_session_factory", None)
        try:
            if sf is not None:
                sess = sf(thread_id)
                hist_fn = getattr(sess, "history", None)
                if callable(hist_fn):
                    history = list(await hist_fn())
        except Exception:
            logger.warning(
                "history fetch via _session_factory failed; falling back to _sessions",
                exc_info=True,
            )

        if not history:
            sess_dict = getattr(runtime, "_sessions", None)
            if isinstance(sess_dict, dict):
                sess = sess_dict.get(thread_id)
                if sess is not None:
                    hist_fn = getattr(sess, "history", None)
                    if callable(hist_fn):
                        try:
                            history = list(await hist_fn())
                        except Exception:
                            logger.warning(
                                "history fetch from _sessions failed; sending empty history",
                                exc_info=True,
                            )
                            history = []

    for msg in history:
        # msg 形态可能是 dict 或 Message 对象
        role = _extract_field(msg, "role", default="user")
        content = _extract_field(msg, "content", default="")
        tool_call_id = _extract_field(msg, "tool_call_id", default=None)
        # 只接受 v0.1.5 协议合法的 role；其它一律标 "assistant"
        if role not in ("user", "assistant", "tool"):
            role = "assistant"
        # Message.content 类型是 ``str | None``（assistant 只发 tool_calls 时为
        # None）。v0.1.6 修：之前 ``str(content)`` 会把 None 转成字面 "None"
        # 传到前端，UI 在用户消息后显示一个白框写着 "None"，体感像 bug。
        # 改成空串兜底——"无文本"的语义就是空，不是字符串 "None"。
        if not isinstance(content, str):
            content = ""
        timestamp = _now_iso_utc()

        tool_calls = _extract_field(msg, "tool_calls", default=None)
        if role == "assistant" and tool_calls:
            if content:
                messages.append(
                    {
                        "id": str(uuid4()),
                        "sessionId": None,
                        "timestamp": timestamp,
                        "provider": "generic_chat",
                        "frame_type": "text",
                        "role": "assistant",
                        "content": content,
                    }
                )
            for call in tool_calls:
                call_id = _extract_field(call, "call_id", default=None)
                tool_name = _extract_field(call, "tool_name", default=None)
                arguments = _extract_field(call, "arguments", default=None)
                messages.append(
                    {
                        "id": str(uuid4()),
                        "sessionId": None,
                        "timestamp": timestamp,
                        "provider": "generic_chat",
                        "frame_type": "tool_use",
                        "toolId": call_id if isinstance(call_id, str) else str(uuid4()),
                        "toolName": tool_name if isinstance(tool_name, str) else "unknown",
                        "toolInput": arguments if isinstance(arguments, dict) else {},
                    }
                )
            continue

        if role == "tool":
            raw_name = _extract_field(msg, "name", default=None)
            tool_name = raw_name if isinstance(raw_name, str) else None
            metadata = _extract_field(msg, "metadata", default=None)
            ok: bool | None = None
            error_message: str | None = None
            if isinstance(metadata, dict):
                meta_ok = metadata.get("ok")
                ok = bool(meta_ok) if isinstance(meta_ok, bool) else None
                meta_err = metadata.get("error_message")
                error_message = meta_err if isinstance(meta_err, str) else None
            messages.append(
                {
                    "id": str(uuid4()),
                    "sessionId": None,
                    "timestamp": timestamp,
                    "provider": "generic_chat",
                    "frame_type": "tool_result",
                    "toolId": tool_call_id if isinstance(tool_call_id, str) else str(uuid4()),
                    "toolName": tool_name if isinstance(tool_name, str) else "unknown",
                    "content": content or error_message or "",
                    "isError": ok is False,
                }
            )
            continue

        out_role: Literal["user", "assistant"] = "assistant"
        if role == "user":
            out_role = "user"
        messages.append(
            {
                "id": str(uuid4()),
                "sessionId": None,
                "timestamp": timestamp,
                "provider": "generic_chat",
                "frame_type": "text",
                "role": out_role,
                "content": content,
            }
        )

    frame = ThreadHistoryFrame(messages=messages, timestamp_ms=_now_ms())
    await websocket.send_json(frame.model_dump())


async def _send_evolution_replay_frames(websocket: WebSocket, cell: Any) -> None:
    cfg = getattr(websocket.app.state, "config", None)
    if cfg is None:
        return
    if getattr(cfg.evolution.learning, "enabled", False) is not True:
        return
    raw_root = getattr(cfg.evolution.learning, "root_path", None)
    if not isinstance(raw_root, str) or not raw_root.strip():
        return
    root_dir = resolve_evolution_root(raw_root)
    store = EvolutionStore(
        root_dir=root_dir,
        state_store=EvolutionStateStore(root_dir),
    )
    snapshots = await store.list_notice_snapshots_for_session(str(cell.thread_id))
    for snapshot in snapshots:
        frame = SystemNoticeFrame(
            notice_key="self_evolution.review",
            source="self_evolution",
            status=snapshot.status,
            title=snapshot.title,
            message=snapshot.message,
            details=dict(snapshot.details),
            icon=snapshot.icon,
            run_id=snapshot.run_id,
            timestamp_ms=snapshot.reviewed_at_ms,
        )
        await websocket.send_json(frame.model_dump())


def _extract_field(obj: Any, name: str, *, default: Any) -> Any:
    """从 dict 或对象上取字段；缺字段返回 default。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


async def _send_error_frame(
    websocket: WebSocket,
    error_code: str,
    message: str,
    *,
    turn: int | None = None,
) -> None:
    """推 :class:`ErrorFrame`。"""
    try:
        frame = ErrorFrame(
            error_code=error_code,  # type: ignore[arg-type]
            message=message,
            turn=turn,
            timestamp_ms=_now_ms(),
        )
        await websocket.send_json(frame.model_dump())
    except Exception as exc:
        log_network_exception(
            "hosts.web.websocket.routes",
            "send_error_frame_failed",
            exc,
            error_code=error_code,
            thread_turn=turn,
        )


async def _send_no_active_run_notice(websocket: WebSocket, thread_id: str) -> None:
    """收到 InterruptFrame 但当前没 active run 时，推一条 :class:`SystemNoticeFrame`。

    interrupt-run-v0.1：用户连点 Stop / Stop 时 race 上 run 自然完成 等场景。
    不报错（不是协议违规），只通知前端"没有可中断的 run"，前端可隐藏 Stop
    按钮 + 显示 toast。
    """
    try:
        frame = SystemNoticeFrame(
            timestamp_ms=_now_ms(),
            notice_key="no_active_run",
            source="ws.interrupt",
            status="info",
            title="无活动任务",
            message="当前 thread 没有正在跑的 run，无需打断。",
            icon="info",
        )
        await websocket.send_json(frame.model_dump())
    except Exception as exc:
        log_network_exception(
            "hosts.web.websocket.routes",
            "send_no_active_run_notice_failed",
            exc,
            thread_id=thread_id,
        )


__all__ = [
    "MAX_FRAME_BYTES",
    "THREAD_ID_RE",
    "WS_CLOSE_POLICY_VIOLATION",
    "register_ws_routes",
]
