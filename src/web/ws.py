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
- 1MB 限制：``len(raw_text)`` 字节而非字符数（中文 UTF-8 约 3 字节 / 字）。
- ``cell.runtime`` 的 session history 由 ``runtime._sessions[thread_id]`` 提供；
  v0.1.5 通过 ``runtime.session_factory`` 兜底拿（需要 :class:`NativeRuntime` 暴露此接口）。
  如果 runtime 接口不直接暴露 history，``thread.history`` 帧降级为空消息列表。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from evolution.state_store import EvolutionStateStore
from evolution.store import EvolutionStore, resolve_evolution_root
from web.auth import SESSION_COOKIE_NAME, verify_session_cookie
from web.protocol import (
    ErrorFrame,
    HistoryMessageDTO,
    PongFrame,
    SystemNoticeFrame,
    ThreadHistoryFrame,
    WSFrameC2SAdapter,
)

if TYPE_CHECKING:
    from itsdangerous import URLSafeTimedSerializer

    from web.types import ThreadManagerProtocol

logger = logging.getLogger(__name__)

THREAD_ID_RE: re.Pattern[str] = re.compile(r"^thread-[a-f0-9]{12}$")
MAX_FRAME_BYTES = 1_000_000  # 1 MB
"""单帧最大字节数（UTF-8 编码后）。"""

WS_CLOSE_POLICY_VIOLATION = 1008


def _now_ms() -> int:
    return int(time.time() * 1000)


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
        # 装配缺失，按 1008 关
        await websocket.close(
            code=WS_CLOSE_POLICY_VIOLATION,
            reason="auth not configured",
        )
        return

    raw_cookie = websocket.cookies.get(SESSION_COOKIE_NAME)
    payload = verify_session_cookie(raw_cookie, serializer)
    if payload is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="not authenticated")
        return

    # 2. thread_id 正则
    if not THREAD_ID_RE.match(thread_id):
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="invalid thread id")
        return

    # 3. boot_or_attach
    tm: ThreadManagerProtocol = websocket.app.state.thread_manager
    try:
        cell = await tm.boot_or_attach(thread_id)
    except KeyError:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="thread not found")
        return
    except Exception:
        logger.exception("boot_or_attach failed for thread_id=%s", thread_id)
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="boot failed")
        return

    # 4. accept
    await websocket.accept()
    cell.attach_ws(websocket)
    cell.touch()

    # 5. 推 thread.history
    try:
        await _send_history_frame(websocket, cell)
        await _send_evolution_replay_frames(websocket, cell)
    except Exception:
        logger.exception("send history frame failed for thread_id=%s", thread_id)
        # 不关连接，只是 history 失败 —— 让用户继续对话

    # 6. 入帧循环
    try:
        await _receive_loop(websocket, cell, tm, thread_id)
    finally:
        # cleanup：只注销当前 ws；cell / adapter / runtime 生命周期继续由
        # ThreadManager 管，避免同一 thread 的其它连接被一条断连带走。
        detach_call = getattr(cell, "detach_ws", None)
        if callable(detach_call):
            with contextlib.suppress(Exception):
                detach_call(websocket)


async def _receive_loop(
    websocket: WebSocket,
    cell: Any,
    tm: ThreadManagerProtocol,
    thread_id: str,
) -> None:
    """C2S 帧分发循环；客户端断连 → :class:`WebSocketDisconnect` → 退出。"""
    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return
        except Exception:
            logger.exception("ws receive_text raised; closing")
            return

        # 1MB 限制（按 UTF-8 字节）
        if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
            await _send_error_frame(websocket, "internal", "frame too large (>1MB)")
            continue

        # JSON 解析 + discriminated union
        try:
            frame = WSFrameC2SAdapter.validate_json(raw)
        except ValidationError as exc:
            await _send_error_frame(websocket, "internal", f"invalid frame: {exc.errors()[:3]}")
            continue
        except Exception as exc:
            await _send_error_frame(websocket, "internal", f"frame parse error: {exc}")
            continue

        cell.touch()
        await _dispatch_frame(frame, cell, websocket, tm, thread_id)


async def _dispatch_frame(
    frame: Any,
    cell: Any,
    websocket: WebSocket,
    tm: ThreadManagerProtocol,
    thread_id: str,
) -> None:
    """分派单个 C2S 帧。"""
    kind = frame.kind
    if kind == "user.input":
        # 后台跑 run_once；不阻塞读循环
        effort = getattr(frame, "reasoning_effort", None)
        task = asyncio.create_task(
            _run_once_safely(cell, frame.text, websocket, reasoning_effort=effort),
            name=f"web-run-once-{thread_id}",
        )
        # 把 task 暂存到 cell（便于 evict 时 cancel）
        cell.current_run_task = task
    elif kind == "approval.ack":
        # v0.1.6 三态：传递字符串字面值给 thread_manager，由它转 ApprovalAction
        # 枚举（thread_manager 在装配层，可 import core.contracts；ws 是 app shell
        # 层不允许）。非法字段降级为 REJECT 由 thread_manager 处理。
        try:
            tm.resolve_approval(thread_id, frame.call_id, frame.action)
        except Exception:
            logger.exception("resolve_approval raised; ignored")
    elif kind == "ping":
        try:
            pong = PongFrame(timestamp_ms=_now_ms())
            await websocket.send_json(pong.model_dump())
        except Exception:
            # 推 pong 失败说明 ws 断了；让下次 receive 抛 WebSocketDisconnect
            pass
    else:
        # discriminated union 已经过滤；这里只是兜底
        await _send_error_frame(websocket, "internal", f"unknown frame kind: {kind}")


async def _run_once_safely(
    cell: Any,
    text: str,
    websocket: WebSocket,
    *,
    reasoning_effort: str | None = None,
) -> None:
    """在后台跑 ``cell.bridge.run_once``；异常推 ``error`` 帧不沉默死掉。

    token 持久化由 :class:`UsagePersistSink` 在每个 turn 的 ``usage``
    event 时增量写盘，不在此处做 run 结束一次性写入。
    """
    try:
        await cell.bridge.run_once(text, reasoning_effort=reasoning_effort)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("run_once failed; emitting error frame")
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
      :class:`HistoryMessageDTO`。``turn`` 字段在 runtime history 里通常没有，
      用 -1 占位，让浏览器按 timestamp_ms 顺序排即可。
    """
    messages: list[HistoryMessageDTO] = []
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
        # v0.1.6：tool 角色补齐 tool_name / ok / data / error_message
        # （前端历史重放路径之前看不到这些信息，刷新页面后工具卡片全空）
        tool_name: str | None = None
        ok: bool | None = None
        data: dict[str, Any] | None = None
        error_message: str | None = None
        if role == "tool":
            raw_name = _extract_field(msg, "name", default=None)
            tool_name = raw_name if isinstance(raw_name, str) else None
            metadata = _extract_field(msg, "metadata", default=None)
            if isinstance(metadata, dict):
                meta_ok = metadata.get("ok")
                ok = bool(meta_ok) if isinstance(meta_ok, bool) else None
                meta_data = metadata.get("data")
                data = meta_data if isinstance(meta_data, dict) else None
                meta_err = metadata.get("error_message")
                error_message = meta_err if isinstance(meta_err, str) else None
        messages.append(
            HistoryMessageDTO(
                role=role,
                content=content,
                turn=-1,
                timestamp_ms=_now_ms(),
                tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
                tool_name=tool_name,
                ok=ok,
                data=data,
                error_message=error_message,
            )
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
    except Exception:
        logger.debug("send error frame failed; ignoring")


__all__ = [
    "MAX_FRAME_BYTES",
    "THREAD_ID_RE",
    "WS_CLOSE_POLICY_VIOLATION",
    "register_ws_routes",
]
