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

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect

from network import get_network_manager
from web._shared.session_manager import SessionManager
from web.auth import SESSION_COOKIE_NAME, verify_session_cookie
from web.auto_approval.ws_handlers import (
    build_auto_approval_state_msg,
    handle_auto_approval_query,
    handle_auto_approval_toggle,
)
from web.claude_code.approval import ApprovalBridge
from web.claude_code.normalizer import ClaudeNormalizer
from web.claude_code.service import ClaudeCodeService
from web.protocol.rest_models import UserInputAttachment

if TYPE_CHECKING:
    from itsdangerous import URLSafeTimedSerializer

    from web.types import ThreadManagerProtocol

logger = logging.getLogger(__name__)

WS_CLOSE_POLICY_VIOLATION = 1008

# 与 web.routers.threads.THREAD_ID_RE 同款；本文件不允许 import routers.threads
# （避免反向依赖），直接复制正则字面量。
_THREAD_ID_RE: re.Pattern[str] = re.compile(r"^thread-[a-f0-9]{12}$")

router = APIRouter()


@router.get("/api/claude-code/test-evolution-event")
async def test_evolution_event(
    request: Request,
    thread_id: str = Query(...),
    status: str = Query(default="completed"),
) -> dict[str, Any]:
    """临时测试端点：往 EvolutionManager 的 EventBus 注入模拟 evolution 帧。"""
    from core.contracts import Event

    manager = getattr(request.app.state, "evolution_manager", None)
    if manager is None:
        return {"error": "evolution_manager not available"}

    import time

    run_id = f"run-claude-{thread_id}-test-{int(time.time())}"
    review_id = f"evo-review:{run_id}"

    if status == "started":
        event = Event(
            kind="evolution.review.started",
            run_id=run_id,
            payload={
                "review_id": review_id,
                "session_id": thread_id,
                "timeout_seconds": 120,
            },
        )
    elif status == "completed":
        event = Event(
            kind="evolution.review.completed",
            run_id=run_id,
            payload={
                "review_id": review_id,
                "session_id": thread_id,
                "nutrients_written": 3,
                "duration_ms": 45000,
                "timeout_hit": False,
                "review_summary": "本轮对话涉及 Git 提交规范和测试隔离经验",
                "nutrient_summaries": [
                    "commit 前必须验证相关测试通过",
                    "路径参数需要 resolve 为绝对路径",
                    "不要用 python -c 做一次性验证",
                ],
            },
        )
    else:
        event = Event(
            kind="evolution.review.failed",
            run_id=run_id,
            payload={
                "review_id": review_id,
                "session_id": thread_id,
                "error": "test failure",
            },
        )

    # 诊断：bus 路由表
    routes = {k: type(v).__name__ for k, v in manager._event_bus._routes.items()}
    sink = manager._event_bus._routes.get(thread_id)
    sink_closed = getattr(sink, "_closed", "N/A") if sink else "no sink"

    await manager._event_bus.emit(event)
    return {
        "ok": True,
        "kind": event.kind,
        "run_id": run_id,
        "thread_id": thread_id,
        "bus_routes": routes,
        "sink_type": type(sink).__name__ if sink else None,
        "sink_closed": sink_closed,
    }


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
    meta: Any = None  # 不传 thread_id 时保持 None（修复 v0.1 时未初始化的 UnboundLocalError）
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

    # evolution manager（频道无关 evolution 子系统门面，可能为 None — 未配置时）
    evolution_manager = getattr(websocket.app.state, "evolution_manager", None)
    # 缓存 thread metadata 字段供 evolution hook 用
    bound_cwd: str = getattr(meta, "cwd", "") if meta is not None else ""
    bound_claude_tid: str = getattr(meta, "claude_thread_id", "") if meta is not None else ""

    normalizer = ClaudeNormalizer()
    # smart-approval-v1 装配：从 app.state 取 policy/audit（未装配时 None → 走老行为）
    auto_approval_policy = getattr(websocket.app.state, "auto_approval_policy", None)
    auto_approval_audit = getattr(websocket.app.state, "auto_approval_audit", None)
    # smart-approval-v2-inbox 装配：全局 inbox broadcaster（未装配时 None → 走 v1 行为，仅本 thread WS 弹窗）
    inbox_broadcaster = getattr(websocket.app.state, "approval_inbox_broadcaster", None)
    approval = ApprovalBridge(
        normalizer,
        sessions,
        policy=auto_approval_policy,
        audit=auto_approval_audit,
        cwd=bound_cwd,
        thread_id=bound_thread_id,
        channel="claude_code",
        inbox_broadcaster=inbox_broadcaster,
    )
    # v2-inbox: 注册 thread_id → bridge 映射，让前端 inbox.resolve 帧能路由回此 bridge
    if inbox_broadcaster is not None and bound_thread_id:
        inbox_broadcaster.register_bridge(bound_thread_id, approval)
    # v0.2 透传 thread_manager（可能为 None — 老路径 / 未配置时仍能运行）
    tm_for_service: ThreadManagerProtocol | None = getattr(
        websocket.app.state,
        "thread_manager",
        None,
    )
    # claude-code-channel-image-paste: 注入 app.state.asset_storage 让 service
    # 能拼 ``@file`` prefix；缺失（测试 / 老装配）走 None → service 跳过 prefix。
    asset_storage_for_service = getattr(websocket.app.state, "asset_storage", None)
    service = ClaudeCodeService(
        normalizer,
        approval,
        sessions,
        thread_manager=tm_for_service,
        asset_storage=asset_storage_for_service,
    )

    # 跟踪 fire-and-forget 后台 task，避免 GC 提前回收（asyncio 文档约定）
    bg_tasks: set[asyncio.Task[Any]] = set()

    await websocket.accept()

    # network-layer-claude-keepalive-v0.1: 注册到 NetworkManager 拿 conn_id，
    # 主循环里每条 inbound 帧先走 network_manager.handle_inbound() 拦下 ping
    # 帧回 pong；走完心跳路径后才进业务 _dispatch。
    network_manager = get_network_manager()
    conn_id = await network_manager.register(
        "claude",
        websocket,
        thread_id=bound_thread_id,
    )

    # 2.7 evolution hook: register event route
    if evolution_manager is not None and evolution_manager.enabled and bound_thread_id:
        from web.ws_event_sink import WSEventSink

        evo_sink = WSEventSink(ws=websocket, thread_id=bound_thread_id)
        evolution_manager.register_event_route(bound_thread_id, evo_sink)

    # 2.8 smart-approval-v1: 连接建立后主动 push 一次 state（前端 toggle 初始状态）
    if auto_approval_policy is not None and bound_cwd:
        with contextlib.suppress(Exception):
            await websocket.send_json(
                build_auto_approval_state_msg(auto_approval_policy, bound_cwd),
            )

    # 3. 主循环
    try:
        while True:
            data = await websocket.receive_json()
            # network-layer v0.1: 心跳帧由 NetworkManager 拦下回 pong；返回
            # True 即跳过业务路由。非 dict 帧（如纯字符串）由 _dispatch 自己
            # 报 error，本路径只对 dict 走 handle_inbound。
            if isinstance(data, dict) and await network_manager.handle_inbound(
                conn_id,
                data,
            ):
                continue
            await _dispatch(
                websocket,
                data,
                service,
                approval,
                sessions,
                bg_tasks,
                bound_thread_id=bound_thread_id,
                evolution_manager=evolution_manager,
                bound_cwd=bound_cwd,
                bound_claude_tid=bound_claude_tid,
            )
    except WebSocketDisconnect:
        logger.debug("claude-code ws disconnected")
    except Exception as exc:
        logger.exception("claude-code ws unhandled error")
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {
                    "frame_type": "error",
                    "provider": "claude",
                    "error": f"unhandled: {exc!r}",
                },
            )
        with contextlib.suppress(Exception):
            await websocket.close()
    finally:
        # network-layer v0.1: 注销连接，停止心跳状态机。幂等：未知 conn_id
        # 静默返回，多次 disconnect / 异常退出都安全。
        with contextlib.suppress(Exception):
            await network_manager.unregister(conn_id)
        # evolution hook: unregister event route on any exit path
        if evolution_manager is not None and evolution_manager.enabled and bound_thread_id:
            evolution_manager.unregister_event_route(bound_thread_id)
        # v2-inbox: unregister bridge（仅在 registry 里是本 bridge 时才删；
        # 多 tab 同 thread 时新连接会覆盖，老连接断时不应误删新的）
        if inbox_broadcaster is not None and bound_thread_id:
            inbox_broadcaster.unregister_bridge(bound_thread_id, approval)


async def _dispatch(
    websocket: WebSocket,
    data: dict[str, Any],
    service: ClaudeCodeService,
    approval: ApprovalBridge,
    sessions: SessionManager,
    bg_tasks: set[asyncio.Task[Any]],
    *,
    bound_thread_id: str | None = None,
    evolution_manager: Any = None,
    bound_cwd: str = "",
    bound_claude_tid: str = "",
) -> None:
    """单条入站帧分发。"""
    msg_type = data.get("frame_type") if isinstance(data, dict) else None

    if msg_type == "claude-command":
        command = data.get("command", "")
        options = data.get("options") or {}
        if not isinstance(command, str):
            await _send_error(websocket, "claude-command.command must be string")
            return

        # claude-code-channel-image-paste: 解析 attachments 字段（前端
        # ClaudeCodeView 粘贴图片后发上来）。容错：
        # - 字段缺失 / None / 非 list / 空 list → attachments=None 走原 prompt 路径
        # - 单条 dict 校验失败由 pydantic 抛 ValidationError，捕获后 fallback 到 None
        raw_attachments = data.get("attachments") if isinstance(data, dict) else None
        parsed_attachments: list[UserInputAttachment] | None = None
        if isinstance(raw_attachments, list) and raw_attachments:
            try:
                parsed_attachments = [
                    UserInputAttachment.model_validate(item) for item in raw_attachments
                ]
            except Exception:
                logger.warning(
                    "claude-command attachments parse failed; falling back to text-only",
                    exc_info=True,
                )
                parsed_attachments = None

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
                attachments=parsed_attachments,
            ),
        )
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

        # evolution hook: fire-and-forget notify_user_message
        if (
            evolution_manager is not None
            and evolution_manager.enabled
            and bound_thread_id
            and bound_claude_tid
        ):
            from evolution.claude_transcript_provider import ClaudeTranscriptProvider

            evo_task = asyncio.create_task(
                evolution_manager.notify_user_message(
                    thread_id=bound_thread_id,
                    provider=ClaudeTranscriptProvider(
                        thread_id=bound_thread_id,
                        claude_thread_id=bound_claude_tid,
                        cwd=bound_cwd,
                    ),
                    cwd=bound_cwd,
                ),
            )
            bg_tasks.add(evo_task)
            evo_task.add_done_callback(bg_tasks.discard)

        return

    if msg_type == "claude-permission-response":
        request_id = data.get("requestId")
        if not isinstance(request_id, str):
            await _send_error(websocket, "claude-permission-response.requestId required")
            return
        approval.resolve(request_id, data)
        return

    if msg_type == "abort-session":
        # interrupt-claude-channel-v0.1：
        # - service.abort() 返回 True（有活 run 被 cancel）→ _consume() 的
        #   CancelledError finally 分支会 emit complete（service.py:192-201），
        #   route 层**不**再发，避免双重 emit 让前端渲染 2 张"对话已中止"
        # - service.abort() 返回 False（无活 run：unknown sid / 已 disconnect /
        #   register 已 unregister）→ route 主动发 complete 兜底，否则前端
        #   等不到任何回应卡死
        session_id = data.get("sessionId")
        if not isinstance(session_id, str):
            await _send_error(websocket, "abort-session.sessionId required")
            return
        aborted = await service.abort(session_id)
        if not aborted:
            await websocket.send_json(
                {
                    "frame_type": "complete",
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
                "frame_type": "session-status",
                "sessionId": session_id,
                "isProcessing": active,
            },
        )
        return

    # smart-approval-v1: per-cwd toggle 开关 + 回执 state
    # 真正的 toggle/query/state 构造逻辑迁到 web.auto_approval.ws_handlers，
    # 让 generic_chat 等其他通道复用。route 这里只负责从 app.state 取 policy
    # 并透传，保留命令路由分发职责。
    if msg_type == "auto-approval-toggle":
        policy = getattr(websocket.app.state, "auto_approval_policy", None)
        await handle_auto_approval_toggle(websocket, data, policy)
        return

    if msg_type == "auto-approval-query":
        # 前端按 cwd 主动拉一次 state（切 thread / 重连后用）
        policy = getattr(websocket.app.state, "auto_approval_policy", None)
        await handle_auto_approval_query(websocket, data, policy)
        return

    await _send_error(websocket, f"unknown command type: {msg_type!r}")


async def _send_error(websocket: WebSocket, error_message: str) -> None:
    """发送 error 帧。"""
    with contextlib.suppress(Exception):
        await websocket.send_json(
            {
                "frame_type": "error",
                "provider": "claude",
                "error": error_message,
            },
        )


__all__ = ["router"]
