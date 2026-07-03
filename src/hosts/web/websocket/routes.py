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
   - ``approval.ack`` → 旧审批帧，generic_chat 主链已迁到 ``approval.inbox.resolve``
   - ``ping``         → 回 ``pong``

   - 帧 > 1MB → 推 ``error`` 帧 + 继续读
   - JSON 解析失败 / 字段不匹配 → 推 ``error`` 帧 + 继续读
   - 客户端断连 → ``cell.adapter`` / ws_event_sink 静默吞 send 失败

设计要点：

- ``run_once`` 用 :func:`asyncio.create_task` 不阻塞读循环；任务出错由
  done_callback 推 ``error`` 帧。
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
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from hosts.web.app_support.generic_history import normalize_generic_history
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
    GenericChatC2SAdapter,
    PongFrame,
    SystemNoticeFrame,
    ThreadHistoryFrame,
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
PENDING_INPUT_QUEUE_FULL_REASON = "pending_input_queue_full"


def _now_ms() -> int:
    return int(time.time() * 1000)


def register_ws_routes(app: FastAPI) -> None:
    """把 WS 端点注册到 FastAPI app（由 :func:`web.app.create_app` 调）。"""

    @app.websocket("/ws/threads/{thread_id}")
    async def thread_ws(websocket: WebSocket, thread_id: str) -> None:
        await handle_thread_ws_channel(websocket, thread_id)

    @app.websocket("/ws/cron/tasks/{task_id}/runs/{run_id}")
    async def cron_run_ws(websocket: WebSocket, task_id: str, run_id: str) -> None:
        await _cron_run_ws_handler(websocket, task_id, run_id)


def _find_cron_run(store: Any, task_id: str, run_id: str) -> Any | None:
    for run in store.list_runs(task_id, limit=None):
        if getattr(run, "run_id", None) == run_id:
            return run
    return None


def _resolve_cron_run_preset_id(websocket: WebSocket, task: Any) -> str:
    candidate = str(getattr(task, "preset_id", "") or "").strip()
    if candidate:
        return candidate

    parent_thread_id = str(getattr(task, "thread_id", "") or "")
    tm = getattr(websocket.app.state, "thread_manager", None)
    if tm is not None and parent_thread_id:
        try:
            for meta in tm.list_threads():
                if getattr(meta, "id", "") == parent_thread_id:
                    preset_id = str(getattr(meta, "preset_id", "") or "").strip()
                    if preset_id:
                        return preset_id
        except Exception:
            logger.warning(
                "failed to resolve cron run preset from parent thread metadata",
                exc_info=True,
            )

    cfg = getattr(websocket.app.state, "config", None)
    presets = list(getattr(getattr(cfg, "web", None), "llm_presets", []) or [])
    if presets:
        return str(getattr(presets[0], "id", "") or "")
    return ""


async def _cron_run_ws_handler(websocket: WebSocket, task_id: str, run_id: str) -> None:
    """处理定时任务 run history 的临时 WS 会话。

    关键输入是 task_id/run_id，输出是一条只服务当前连接的 ephemeral cell。该路径不
    读写 thread metadata，也不接入 pending input queue；断开连接时由本函数关闭
    adapter 和 runtime。
    """
    serializer: URLSafeTimedSerializer | None = getattr(websocket.app.state, "serializer", None)
    if serializer is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="auth not configured")
        return

    raw_cookie = websocket.cookies.get(SESSION_COOKIE_NAME)
    payload = verify_session_cookie(raw_cookie, serializer)
    if payload is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="not authenticated")
        return

    store = getattr(websocket.app.state, "scheduler_store", None)
    if store is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="scheduler not configured")
        return

    task = store.get_task(task_id)
    if task is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="task not found")
        return
    run = _find_cron_run(store, task_id, run_id)
    if run is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="run not found")
        return

    session_id = str(getattr(run, "session_id", "") or "").strip()
    if not session_id:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="run session missing")
        return
    preset_id = _resolve_cron_run_preset_id(websocket, task)
    if not preset_id:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="preset missing")
        return

    tm: ThreadManagerProtocol = websocket.app.state.thread_manager
    try:
        cell = await tm.build_ephemeral_session_cell(
            session_id=session_id,
            preset_id=preset_id,
        )
    except Exception as exc:
        logger.exception("build cron run cell failed for task=%s run=%s", task_id, run_id)
        log_generic_channel_exception(
            "cron_run_boot_failed",
            exc,
            level="ERROR",
            thread_id=session_id,
            task_id=task_id,
            run_id=run_id,
        )
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="boot failed")
        return

    await websocket.accept()
    network_manager = getattr(websocket.app.state, "network_manager", None)
    if network_manager is None:
        network_manager = get_network_manager()
    conn_id = await network_manager.register("cron-run", websocket, session_id)
    log_generic_channel_event(
        "cron_run_registered",
        thread_id=session_id,
        conn_id=conn_id,
        task_id=task_id,
        run_id=run_id,
    )
    cell.attach_ws(websocket)
    cell.touch()

    try:
        await _send_history_frame(websocket, cell)
        await _receive_loop(websocket, cell, tm, session_id, network_manager, conn_id)
    finally:
        with contextlib.suppress(Exception):
            await network_manager.unregister(conn_id)
        with contextlib.suppress(Exception):
            cell.detach_ws(websocket)
        with contextlib.suppress(Exception):
            await tm.close_ephemeral_session_cell(cell, reason="session_close")


async def handle_thread_ws_channel(
    websocket: WebSocket,
    thread_id: str,
    *,
    network_channel: str = "generic",
    require_cookie: bool = True,
    allowed_backend_kind: str | None = None,
    include_evolution_replay: bool = True,
) -> None:
    """处理 thread WS channel 连接。

    关键输入：WebSocket、thread_id、网络频道名、鉴权开关和允许的 backend_kind。
    关键输出：连接生命周期进入通用 C2S/S2C frame 循环。
    """
    await _thread_ws_handler(
        websocket,
        thread_id,
        network_channel=network_channel,
        require_cookie=require_cookie,
        allowed_backend_kind=allowed_backend_kind,
        include_evolution_replay=include_evolution_replay,
    )


async def _thread_ws_handler(
    websocket: WebSocket,
    thread_id: str,
    *,
    network_channel: str = "generic",
    require_cookie: bool = True,
    allowed_backend_kind: str | None = None,
    include_evolution_replay: bool = True,
) -> None:
    """WS 连接生命周期：鉴权 → boot → 推 history → 入帧循环 → cleanup。"""
    # 1. cookie 验
    if require_cookie:
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
    if allowed_backend_kind is not None and not _thread_has_backend_kind(
        tm,
        thread_id,
        allowed_backend_kind,
    ):
        log_generic_channel_event(
            "thread_rejected",
            level="WARNING",
            thread_id=thread_id,
            reason="invalid_backend_kind",
            expected_backend_kind=allowed_backend_kind,
        )
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="invalid thread kind")
        return

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
    conn_id = await network_manager.register(network_channel, websocket, thread_id)
    log_generic_channel_event("registered", thread_id=thread_id, conn_id=conn_id)
    cell.attach_ws(websocket)
    cell.touch()

    # 5. 推 thread.history
    try:
        await _send_history_frame(websocket, cell)
        await _send_pending_input_snapshot(websocket, tm, thread_id)
        if include_evolution_replay:
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


def _thread_has_backend_kind(
    tm: ThreadManagerProtocol,
    thread_id: str,
    backend_kind: str,
) -> bool:
    """判断 thread 是否存在且属于指定 backend_kind。

    关键输入：ThreadManagerProtocol、thread_id 和目标 backend_kind。
    关键输出：匹配时返回 True，缺失或类型不匹配时返回 False。
    """
    list_threads = getattr(tm, "list_threads", None)
    if not callable(list_threads):
        return False
    for metadata in list_threads():
        if getattr(metadata, "id", None) == thread_id:
            return getattr(metadata, "backend_kind", None) == backend_kind
    return False


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

        peek_type: str | None = None
        if isinstance(data, dict):
            t = data.get("frame_type")
            if isinstance(t, str):
                peek_type = t

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

        # JSON 解析 + generic_chat discriminated union
        try:
            frame = GenericChatC2SAdapter.validate_python(data)
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
    """分派单个 C2S 帧。

    user.input / choice.submit 会优先走 ThreadManager 的 pending input queue；
    只有 ephemeral cell 这类无队列状态的连接回落到 direct run helper。队列入口的
    异步拒绝会转成稳定 error 帧，供前端恢复草稿。

    状态归属：本函数只解析 wire frame 并调用 ThreadManager Protocol；队列排序、
    drain、版本号和广播全部由 manager 维护。
    """
    frame_type = frame.frame_type
    if frame_type in {"auto-approval-toggle", "auto-approval-query"}:
        policy = getattr(websocket.app.state, "auto_approval_policy", None)
        payload = frame.model_dump()
        if frame_type == "auto-approval-toggle":
            await handle_auto_approval_toggle(
                websocket,
                payload,
                policy,
                channel="generic_chat",
            )
        else:
            await handle_auto_approval_query(
                websocket,
                payload,
                policy,
                channel="generic_chat",
            )
        return

    if frame_type == "user.input":
        ensure_runtime = getattr(tm, "ensure_cell_runtime_preset_current", None)
        if callable(ensure_runtime):
            refreshed = await ensure_runtime(thread_id)
            if refreshed is False:
                await _send_error_frame(
                    websocket,
                    "internal",
                    "模型切换尚未完成，runtime 刷新失败；请稍后重试。",
                )
                return
        effort = getattr(frame, "reasoning_effort", None)
        # claude-image-paste-e2e #20：把 UserInputAttachment(BaseModel) 列表
        # 提前打成 dict，全链路（runtime / runner / Message.metadata / assembler
        # / provider）保持 dict 形态，避免下游再处理 BaseModel ↔ dict 双形态。
        raw_attachments = getattr(frame, "attachments", None)
        attachments_dicts: list[dict[str, Any]] | None = (
            [a.model_dump() for a in raw_attachments] if raw_attachments else None
        )
        raw_references = getattr(frame, "references", None)
        reference_dicts: list[dict[str, Any]] | None = (
            [r.model_dump() for r in raw_references] if raw_references else None
        )
        if _supports_pending_input_queue(cell):
            try:
                result = await tm.submit_user_input(
                    thread_id,
                    frame.text,
                    request_id=getattr(frame, "request_id", None),
                    reasoning_effort=effort,
                    attachments=attachments_dicts,
                    references=reference_dicts,
                    source_conn_id=conn_id,
                )
            except Exception as exc:
                await _send_submit_error(websocket, exc)
                log_generic_channel_exception(
                    "user_input_rejected",
                    exc,
                    level="WARNING",
                    thread_id=thread_id,
                    conn_id=conn_id,
                    frame_type=frame_type,
                    request_id=getattr(frame, "request_id", None),
                )
                return
            started = getattr(result, "started", False)
        else:
            current_task: asyncio.Task[Any] | None = getattr(cell, "current_run_task", None)
            if current_task is not None and not current_task.done():
                await _send_error_frame(websocket, "internal", "当前任务正在运行，请稍后重试。")
                log_generic_channel_event(
                    "user_input_rejected",
                    level="WARNING",
                    thread_id=thread_id,
                    conn_id=conn_id,
                    frame_type=frame_type,
                    request_id=getattr(frame, "request_id", None),
                    reason="active_run",
                )
                return
            _start_run_once_task(
                cell,
                thread_id,
                websocket,
                frame.text,
                reasoning_effort=effort,
                attachments=attachments_dicts,
                references=reference_dicts,
            )
            started = True
        log_generic_channel_event(
            "user_input_submitted",
            thread_id=thread_id,
            conn_id=conn_id,
            frame_type=frame_type,
            request_id=getattr(frame, "request_id", None),
            started=started,
            text_len=len(frame.text),
            reasoning_effort=effort,
            attachment_count=len(attachments_dicts) if attachments_dicts else 0,
            reference_count=len(reference_dicts) if reference_dicts else 0,
        )
    elif frame_type == "choice.submit":
        try:
            choice_text = format_choice_submit_as_user_input(frame)
        except ValueError as exc:
            await _send_error_frame(websocket, "internal", str(exc))
            log_generic_channel_event(
                "choice_submit_rejected",
                level="WARNING",
                thread_id=thread_id,
                conn_id=conn_id,
                frame_type=frame_type,
                request_id=getattr(frame, "request_id", None),
                reason="invalid_payload",
                error=str(exc),
            )
            return
        if _supports_pending_input_queue(cell):
            try:
                result = await tm.submit_choice_result(
                    thread_id,
                    choice_text,
                    request_id=getattr(frame, "request_id", ""),
                )
            except Exception as exc:
                await _send_submit_error(websocket, exc)
                log_generic_channel_exception(
                    "choice_submit_rejected",
                    exc,
                    level="WARNING",
                    thread_id=thread_id,
                    conn_id=conn_id,
                    frame_type=frame_type,
                    request_id=getattr(frame, "request_id", None),
                )
                return
            started = getattr(result, "started", False)
        else:
            current_task = getattr(cell, "current_run_task", None)
            if current_task is not None and not current_task.done():
                await _send_error_frame(websocket, "internal", "当前任务正在运行，请稍后重试。")
                log_generic_channel_event(
                    "choice_submit_rejected",
                    level="WARNING",
                    thread_id=thread_id,
                    conn_id=conn_id,
                    frame_type=frame_type,
                    request_id=getattr(frame, "request_id", None),
                    reason="active_run",
                )
                return
            _start_run_once_task(cell, thread_id, websocket, choice_text)
            started = True
        log_generic_channel_event(
            "choice_submit_submitted",
            thread_id=thread_id,
            conn_id=conn_id,
            frame_type=frame_type,
            request_id=getattr(frame, "request_id", None),
            started=started,
            answer_count=len(getattr(frame, "answers", []) or []),
            text_len=len(choice_text),
        )
    elif frame_type == "pending-input.update":
        # 编辑帧只修改尚未启动的队列项；服务端返回 changed 快照后前端覆盖本地状态。
        try:
            await tm.update_pending_input(thread_id, frame.pending_input_id, frame.content)
        except Exception as exc:
            await _send_submit_error(websocket, exc)
    elif frame_type == "pending-input.cancel":
        # 删除帧只作用于 pending_inputs 列表；active run 的取消仍走 interrupt 分支。
        try:
            await tm.cancel_pending_input(thread_id, frame.pending_input_id)
        except Exception as exc:
            await _send_submit_error(websocket, exc)
    elif frame_type == "pending-input.reorder":
        # 拖拽排序只在松手后发最终顺序；集合校验和 sequence 重写由 ThreadManager 裁决。
        try:
            await tm.reorder_pending_inputs(thread_id, frame.ordered_ids)
        except Exception as exc:
            await _send_submit_error(websocket, exc)
    elif frame_type == "interrupt":
        # interrupt-run-v0.1：浏览器点 Stop。检查当前 run 是否真在跑：
        # - None / 已 done：推 system notice "no_active_run"，不 cancel
        # - 否则 cancel run → runner 顶层 except → emit run.cancelled
        #   → WSEventSink fanout 转 RunInterruptedFrame（多 tab 自动同步）
        #
        # agent-tree-v0.1（task-4）：cell 已装配 root_agent（agent_loop 副路径启用）时，
        # 走 tree-aware interrupt（tm.interrupt_agent_tree: cancel_subtree + bump_epoch
        # + purge 旧世代内部 mail）；否则保留旧 current_run_task.cancel() 路径（并存）。
        root_agent = getattr(cell, "root_agent", None)
        tree_interrupt = getattr(tm, "interrupt_agent_tree", None)
        if root_agent is not None and callable(tree_interrupt):
            # tree-aware 路径：cancel_subtree 砍 root run_task + bump_epoch + purge。
            did_cancel = await tree_interrupt(thread_id, reason="user_interrupt")
            if not did_cancel:
                # 无在途 run：推 no_active_run（与旧路径一致）。
                await _send_no_active_run_notice(websocket, thread_id)
                log_generic_channel_event(
                    "interrupt_no_active_run",
                    thread_id=thread_id,
                    conn_id=conn_id,
                    frame_type=frame_type,
                    run_id=getattr(frame, "run_id", None),
                )
            else:
                logger.info(
                    "interrupt requested for thread=%s; cancel_subtree + bump_epoch",
                    thread_id,
                )
                log_generic_channel_event(
                    "interrupt_requested",
                    thread_id=thread_id,
                    conn_id=conn_id,
                    frame_type=frame_type,
                    run_id=getattr(frame, "run_id", None),
                )
        else:
            # 旧路径（current_run_task.cancel）：cell 未装配 root_agent 时走此分支。
            interrupt_task: asyncio.Task[Any] | None = getattr(cell, "current_run_task", None)
            if interrupt_task is None or interrupt_task.done():
                await _send_no_active_run_notice(websocket, thread_id)
                log_generic_channel_event(
                    "interrupt_no_active_run",
                    thread_id=thread_id,
                    conn_id=conn_id,
                    frame_type=frame_type,
                    run_id=getattr(frame, "run_id", None),
                )
            else:
                interrupt_task.cancel()
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
        log_generic_channel_event(
            "approval_ack_retired",
            level="WARNING",
            thread_id=thread_id,
            conn_id=conn_id,
            frame_type=frame_type,
            call_id=frame.call_id,
            action=frame.action,
        )
        await _send_error_frame(
            websocket,
            "internal",
            "approval.ack 已下线，请通过全局审批 inbox 处理。",
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
        references = getattr(frame, "references", None)
        return {
            "request_id": getattr(frame, "request_id", None),
            "text_len": len(getattr(frame, "text", "") or ""),
            "reasoning_effort": getattr(frame, "reasoning_effort", None),
            "attachment_count": len(attachments) if attachments else 0,
            "reference_count": len(references) if references else 0,
        }
    if frame_type == "approval.ack":
        return {
            "call_id": getattr(frame, "call_id", None),
            "action": getattr(frame, "action", None),
        }
    if frame_type == "interrupt":
        return {"run_id": getattr(frame, "run_id", None)}
    if frame_type == "choice.submit":
        answers = getattr(frame, "answers", None)
        return {
            "request_id": getattr(frame, "request_id", None),
            "answer_count": len(answers) if answers else 0,
        }
    if frame_type == "ping":
        return {"client_ts": getattr(frame, "ts", None)}
    return {}


def _supports_pending_input_queue(cell: Any) -> bool:
    """判断当前 cell 是否属于 metadata thread 队列状态机。

    ThreadCell 拥有 pending_input_lock；临时 session cell 没有该字段，因此继续使用
    单次 direct run 语义。

    输入是已 boot 的 cell；输出决定 user.input 分支走队列入口还是 direct run。
    """
    return hasattr(cell, "pending_input_lock")


def _start_run_once_task(
    cell: Any,
    thread_id: str,
    websocket: WebSocket,
    text: str,
    *,
    reasoning_effort: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    references: list[dict[str, Any]] | None = None,
) -> asyncio.Task[Any]:
    """创建并登记一次后台 direct run_once task。

    该 helper 只服务无 pending queue 的 ephemeral cell；普通 metadata thread 由
    ThreadManager._start_pending_input_run 统一启动和 drain。完成回调只清理
    current_run_task，不触发队列消费。
    """
    task = asyncio.create_task(
        _run_once_safely(
            cell,
            text,
            websocket,
            reasoning_effort=reasoning_effort,
            attachments=attachments,
            references=references,
        ),
        name=f"web-run-once-{thread_id}",
    )
    cell.current_run_task = task

    def _clear_run_task(
        t: asyncio.Task[Any], *, _cell: Any = cell, _task: asyncio.Task[Any] = task
    ) -> None:
        if getattr(_cell, "current_run_task", None) is _task:
            _cell.current_run_task = None

    task.add_done_callback(_clear_run_task)
    return task


def format_choice_submit_as_user_input(frame: Any) -> str:
    """把 ChoiceSubmitFrame 转成稳定的下一轮用户消息文本。"""
    request_id = str(getattr(frame, "request_id", "") or "").strip()
    if not request_id:
        raise ValueError("choice.submit.request_id must be a non-empty string")
    answers = list(getattr(frame, "answers", []) or [])
    if not answers:
        raise ValueError("choice.submit.answers must contain at least one item")

    lines = [
        "用户已完成选择：",
        f"request_id: {request_id}",
        "",
    ]
    for index, answer in enumerate(answers, start=1):
        question_id = str(getattr(answer, "question_id", "") or "").strip()
        option_id = str(getattr(answer, "option_id", "") or "").strip()
        option_label = str(getattr(answer, "option_label", "") or "").strip()
        custom_text_raw = getattr(answer, "custom_text", None)
        custom_text = custom_text_raw.strip() if isinstance(custom_text_raw, str) else ""
        if not question_id:
            raise ValueError(f"choice.submit.answers[{index - 1}].question_id is required")
        if not option_id:
            raise ValueError(f"choice.submit.answers[{index - 1}].option_id is required")
        if not option_label:
            raise ValueError(f"choice.submit.answers[{index - 1}].option_label is required")
        if option_id == "__custom__" and not custom_text:
            raise ValueError(
                f"choice.submit.answers[{index - 1}].custom_text is required for __custom__"
            )

        lines.append(f"{index}. question_id={question_id}")
        lines.append(f"选择：{option_id} / {option_label}")
        if custom_text:
            lines.append(f"自定义：{custom_text}")
        value = getattr(answer, "value", None)
        if isinstance(value, dict) and value:
            value_text = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if len(value_text) > 1000:
                value_text = value_text[:1000] + "...[truncated]"
            lines.append(f"value: {value_text}")
        lines.append("")
    return "\n".join(lines).rstrip()


async def _run_once_safely(
    cell: Any,
    text: str,
    websocket: WebSocket,
    *,
    reasoning_effort: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    references: list[dict[str, Any]] | None = None,
) -> None:
    """在后台跑 ``cell.bridge.run_once``；异常推 ``error`` 帧不沉默死掉。

    token 持久化由 :class:`UsagePersistSink` 在每个 turn 的 ``usage``
    event 时增量写盘，不在此处做 run 结束一次性写入。

    ``attachments`` / ``references`` 都是 Pydantic DTO ``model_dump()`` 后的
    dict 列表；为 None 表示本轮无对应输入。它们一路透传到
    :meth:`core.runner.Runner._seed_messages`，最终写入 user
    :class:`core.message.Message` 的 metadata。
    """
    try:
        await cell.bridge.run_once(
            text,
            reasoning_effort=reasoning_effort,
            attachments=attachments,
            references=references,
        )
        log_generic_channel_event(
            "run_once_completed",
            thread_id=getattr(cell, "thread_id", None),
            text_len=len(text),
            reasoning_effort=reasoning_effort,
            attachment_count=len(attachments) if attachments else 0,
            reference_count=len(references) if references else 0,
        )
    except asyncio.CancelledError:
        log_generic_channel_event(
            "run_once_cancelled",
            thread_id=getattr(cell, "thread_id", None),
            text_len=len(text),
            reasoning_effort=reasoning_effort,
            attachment_count=len(attachments) if attachments else 0,
            reference_count=len(references) if references else 0,
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
            reference_count=len(references) if references else 0,
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

    messages = normalize_generic_history(history)
    frame = ThreadHistoryFrame(messages=messages, timestamp_ms=_now_ms())
    await websocket.send_json(frame.model_dump())


async def _send_evolution_replay_frames(websocket: WebSocket, cell: Any) -> None:
    manager = getattr(websocket.app.state, "evolution_manager", None)
    if manager is None:
        return
    if getattr(manager, "enabled", False) is not True:
        return
    snapshots = await manager.list_notice_snapshots_for_session(str(cell.thread_id))
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


async def _send_pending_input_snapshot(
    websocket: WebSocket,
    tm: ThreadManagerProtocol,
    thread_id: str,
) -> None:
    """推当前 pending input 队列快照。

    WS 建连后调用，输出完整 snapshot 帧；前端用 version 与 thread_id 合并状态。
    该帧是连接级补偿机制，覆盖断线期间的队列增删改和 drain 结果。
    """
    snapshot = await tm.pending_input_snapshot(thread_id)
    await websocket.send_json(snapshot.model_dump())


async def _send_submit_error(websocket: WebSocket, exc: Exception) -> None:
    """把提交入口错误转换为稳定 WS error 帧。

    pending_input_queue_full 会带 reason，前端据此恢复最近一次 Composer 草稿；其他
    异常统一按 internal message 返回。
    """
    reason = str(getattr(exc, "reason", "") or "")
    if reason == PENDING_INPUT_QUEUE_FULL_REASON:
        await _send_error_frame(
            websocket,
            "internal",
            "队列已满，最多支持 20 条待发送消息。",
            reason=PENDING_INPUT_QUEUE_FULL_REASON,
        )
        return
    await _send_error_frame(websocket, "internal", str(exc))


async def _send_error_frame(
    websocket: WebSocket,
    error_code: str,
    message: str,
    *,
    turn: int | None = None,
    reason: str | None = None,
) -> None:
    """推 :class:`ErrorFrame`。"""
    try:
        frame = ErrorFrame(
            error_code=error_code,  # type: ignore[arg-type]
            message=message,
            turn=turn,
            reason=reason,
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
