"""web — thread-status 全局 Manager + ``/ws/thread-status`` 端点。

设计目的：让所有打开 web app 的客户端实时感知每个 thread 的运行阶段
（idle / responding / thinking / tool_calling / waiting_approval / complete / error），
用于 thread 列表的状态标签渲染。

关键设计：

- 独立端点 ``/ws/thread-status``
- 鉴权：复用 thread WS 的 cookie-based session
- 连接管理：单例 :class:`ThreadStatusManager` 维护 active snapshot、run lease
  和每连接唯一 writer
- 状态发布：producer 先取得 run lease，再把 canonical phase 交给 Manager
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from core.result import RunEndReason
from hosts.web.auth.middleware import SESSION_COOKIE_NAME, verify_session_cookie
from hosts.web.protocol import (
    ApprovalInboxResolveResultFrame,
    PongFrame,
    ThreadStatusC2SAdapter,
)
from hosts.web.websocket.thread_status_manager import (
    ThreadStatusManager,
    ThreadStatusRunLease,
)
from network.network_log import log_network_event

logger = logging.getLogger(__name__)

WS_CLOSE_POLICY_VIOLATION = 1008


def _normalize_origin(value: str | None) -> str | None:
    """归一化 HTTP origin，非法值返回空。"""
    if value is None:
        return None
    raw = value.strip().rstrip("/")
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _request_origin(websocket: WebSocket) -> str | None:
    """从 WebSocket 请求本身推导同源 HTTP origin。"""
    host = websocket.headers.get("host")
    if not host:
        return None
    scheme = "https" if websocket.url.scheme == "wss" else "http"
    return _normalize_origin(f"{scheme}://{host}")


def _configured_origin(websocket: WebSocket) -> str | None:
    """读取配置里的外部 Web origin。"""
    cfg = getattr(websocket.app.state, "config", None)
    raw = getattr(getattr(cfg, "web", None), "server_origin", None)
    return _normalize_origin(raw) if isinstance(raw, str) else None


def _is_allowed_ws_origin(websocket: WebSocket) -> bool:
    """校验浏览器 WebSocket Origin。

    无 Origin 的本地客户端和测试客户端继续放行；带 Origin 的浏览器连接必须
    匹配当前请求 origin 或配置的外部 Web origin。
    """
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    normalized = _normalize_origin(origin)
    if normalized is None:
        return False
    allowed = {_request_origin(websocket), _configured_origin(websocket)}
    return normalized in {item for item in allowed if item is not None}


def _now_ms() -> int:
    """当前时间戳（毫秒）。"""
    return int(time.time() * 1000)


Phase = Literal[
    "idle",
    "responding",
    "thinking",
    "tool_calling",
    "waiting_approval",
    "complete",
    "error",
]

# kind → phase 映射表（stream_status 特殊处理，不在此表）
_KIND_TO_PHASE: dict[str, Phase] = {
    "permission_request": "waiting_approval",
    "permission_cancelled": "idle",
    "complete": "complete",
    "error": "error",
}

# stream_status 的 phase 白名单
_STREAM_STATUS_PHASES: set[str] = {"responding", "thinking", "tool_calling"}

# generic_chat EventSink event.kind → phase 映射
_EVENT_KIND_TO_PHASE: dict[str, Phase] = {
    "turn.start": "responding",
    "content.delta": "responding",
    "reasoning.delta": "thinking",
    "tool.call.start": "tool_calling",
    "run.cancelled": "idle",
    "turn.end": "complete",
    "error": "error",
}

# run.end 结束原因 bitmask → thread-status phase 映射（错误分类器真源）。
#
# 优先级：INTERRUPT 位 set 时映成 idle（尊重用户介入，前端按钮复位为发送）；
# 否则按自然因映射：COMPLETE→complete、MAX_TURNS→complete（预算耗尽不是错误，
# 不该红条）、ERROR→error。
#
# 关键修复：旧实现把 status 三态直接映射（failed→error），导致 max_turns（也是
# failed）被错映成 error phase。现在读 run_end_reason bitmask 精确区分。
# run.end 帧也带 run_end_reason 原值透传给前端，供按钮复位与 UI 显示。
#
# 位常量真源 = core.result.RunEndReason（本模块直接 import，无重复定义）。
_COMPLETE_PHASE: Phase = "complete"
_MAX_TURNS_PHASE: Phase = "complete"
_ERROR_PHASE: Phase = "error"
_INTERRUPT_PHASE: Phase = "idle"


def _phase_from_run_end_reason(reason_int: int) -> Phase | None:
    """从 run_end_reason bitmask 推导终态 phase（优先 INTERRUPT，再按自然因）。"""
    reason = RunEndReason(reason_int)
    if reason == RunEndReason.NONE:
        return None
    if RunEndReason.INTERRUPT in reason:
        return _INTERRUPT_PHASE
    if RunEndReason.MAX_TURNS in reason:
        return _MAX_TURNS_PHASE
    if RunEndReason.ERROR in reason:
        return _ERROR_PHASE
    if RunEndReason.COMPLETE in reason:
        return _COMPLETE_PHASE
    # EVICTED 单独在场（无自然因）——线程被回收，映成 idle 让前端复位。
    return _INTERRUPT_PHASE


def _phase_from_event(kind: str, payload: Any) -> Phase | None:
    """把 Runner Event 映射为 thread-status phase。"""
    if kind == "run.end":
        if not isinstance(payload, dict):
            return None
        reason_raw = payload.get("run_end_reason")
        if isinstance(reason_raw, int):
            return _phase_from_run_end_reason(reason_raw)
        # 兼容旧 payload（无 run_end_reason 字段）——退化到 status 三态映射。
        status = payload.get("status")
        if status == "completed":
            return _COMPLETE_PHASE
        if status == "cancelled":
            return _INTERRUPT_PHASE
        return _ERROR_PHASE if status == "failed" else None
    return _EVENT_KIND_TO_PHASE.get(kind)


async def publish_normalized_status(
    manager: ThreadStatusManager,
    lease: ThreadStatusRunLease,
    normalized: dict[str, Any],
) -> bool:
    """把 Claude/Codex normalized message 映射为携带 lease 的状态增量。"""
    frame_type = normalized.get("frame_type")
    if not isinstance(frame_type, str):
        return False

    phase: Phase | None = None
    if frame_type == "stream_status":
        raw_phase = normalized.get("phase")
        if isinstance(raw_phase, str) and raw_phase in _STREAM_STATUS_PHASES:
            phase = cast(Phase, raw_phase)
    else:
        phase = _KIND_TO_PHASE.get(frame_type)
    if phase is None:
        return False

    tool_name: str | None = None
    if phase == "tool_calling":
        raw_tool_name = normalized.get("toolName")
        tool_name = raw_tool_name if isinstance(raw_tool_name, str) else None
    return await manager.publish_status(
        lease,
        phase=phase,
        tool_name=tool_name,
    )


class ThreadStatusEventSink:
    """generic_chat 路径的 EventSink 实现——把 Runner 事件映射到 thread-status phase。

    只在 phase 变化时广播，避免 ``content.delta`` / ``reasoning.delta``
    每帧都发（高频事件节流）。
    """

    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id
        self._manager = get_thread_status_manager()
        self._leases: dict[str, ThreadStatusRunLease] = {}
        self._last_phase_by_run: dict[str, Phase] = {}

    async def emit(self, event: Any) -> None:
        """满足 ``core.contracts.EventSink`` Protocol。

        终态帧（run.end）bypass 节流强制广播：run 结束是状态机的终态信号，
        必须可靠送达前端，否则停止按钮卡红、刷新无效。普通 delta 帧仍节流。
        """
        kind = getattr(event, "kind", None)
        if not isinstance(kind, str):
            return

        payload = getattr(event, "payload", None) or {}
        phase = _phase_from_event(kind, payload)
        if phase is None:
            return
        raw_run_id = getattr(event, "run_id", None)
        if not isinstance(raw_run_id, str) or not raw_run_id:
            return
        lease = self._leases.get(raw_run_id)
        if lease is None:
            lease = await self._manager.begin_run(self._thread_id, raw_run_id)
            self._leases[raw_run_id] = lease

        # run.end 是终态帧——无论 _last_phase 是否相同都必须广播。
        # 关键修复：旧实现靠 phase 变化节流，但 run.end 可能因竞态产生的 phase
        # 与上一帧相同（如 tool_calling→idle 被节流后 run.end 也是 idle），
        # 导致终态信号被吞，前端按钮永远不复位。
        is_terminal = kind == "run.end"
        if not is_terminal and phase == self._last_phase_by_run.get(raw_run_id):
            return
        self._last_phase_by_run[raw_run_id] = phase

        tool_name: str | None = None
        if phase == "tool_calling":
            raw_tool_name = payload.get("tool_name")
            tool_name = raw_tool_name if isinstance(raw_tool_name, str) else None

        run_end_reason: int | None = None
        if is_terminal and isinstance(payload, dict):
            reason_raw = payload.get("run_end_reason")
            if isinstance(reason_raw, int):
                run_end_reason = reason_raw

        await self._manager.publish_status(
            lease,
            phase=phase,
            tool_name=tool_name,
            run_end_reason=run_end_reason,
        )
        if is_terminal:
            self._leases.pop(raw_run_id, None)
            self._last_phase_by_run.pop(raw_run_id, None)


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------

_manager_singleton: ThreadStatusManager | None = None


def get_thread_status_manager() -> ThreadStatusManager:
    """获取或创建进程内共享 :class:`ThreadStatusManager`。"""
    global _manager_singleton
    if _manager_singleton is None:
        _manager_singleton = ThreadStatusManager()
    return _manager_singleton


def reset_broadcaster_for_testing() -> None:
    """仅测试用：重置 thread status Manager 单例。"""
    global _manager_singleton
    _manager_singleton = None


@dataclass(eq=False)
class _ManagerBackedSender:
    """把 approval inbox 的 send_json 转入 ThreadStatusManager 单 writer。"""

    manager: ThreadStatusManager
    websocket: WebSocket

    async def send_json(self, payload: dict[str, Any]) -> None:
        """向指定连接的 Manager 队列发送 payload。"""
        accepted = await self.manager.send_to(self.websocket, payload)
        if not accepted:
            raise RuntimeError("thread status connection is detached")


# ---------------------------------------------------------------------------
# WS endpoint
# ---------------------------------------------------------------------------


def register_thread_status_routes(app: FastAPI) -> None:
    """把 ``/ws/thread-status`` 端点注册到 FastAPI app。"""

    @app.websocket("/ws/thread-status")
    async def thread_status_ws(websocket: WebSocket) -> None:
        await _thread_status_ws_handler(websocket)


async def _thread_status_ws_handler(websocket: WebSocket) -> None:
    """``/ws/thread-status`` 连接生命周期：鉴权 → accept → attach → receive 循环 → detach。

    smart-approval-v2-inbox：本端点除原 thread-status broadcaster 之外，**同时挂载
    ApprovalInboxBroadcaster**——所有 approval.inbox.* 帧也走这条 WS（端点名是
    历史，现在职责更广；URL 不变是为了不动小绿球 connectionStatus / 客户端 hook）。

    入帧支持 3 种 frame_type：
    - ``ping`` → 回 pong（心跳）
    - ``approval.inbox.resolve`` → 路由到 ApprovalInboxBroadcaster.resolve → manager
    - 其他 → 静默丢弃
    """
    # 延迟 import 避免循环依赖（thread_status_ws 是基础模块，被多处依赖）
    from hosts.web.approvals.global_inbox import get_inbox_broadcaster

    # 1. cookie 鉴权
    serializer = getattr(websocket.app.state, "serializer", None)
    if serializer is None:
        await websocket.close(
            code=WS_CLOSE_POLICY_VIOLATION,
            reason="auth not configured",
        )
        return

    if not _is_allowed_ws_origin(websocket):
        await websocket.close(
            code=WS_CLOSE_POLICY_VIOLATION,
            reason="invalid origin",
        )
        return

    raw_cookie = websocket.cookies.get(SESSION_COOKIE_NAME)
    payload = verify_session_cookie(raw_cookie, serializer)
    if payload is None:
        await websocket.close(
            code=WS_CLOSE_POLICY_VIOLATION,
            reason="not authenticated",
        )
        return

    # 2. accept + attach 两个 broadcaster
    await websocket.accept()
    manager = get_thread_status_manager()
    inbox = get_inbox_broadcaster()
    inbox_sender = _ManagerBackedSender(manager, websocket)
    await manager.attach(websocket)
    await inbox.attach(inbox_sender)

    # 2.5 连接建立时主动 push inbox snapshot（让新连接看到当前所有 pending 审批）
    await inbox.push_snapshot(inbox_sender)

    # 3. 入帧循环
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue  # 非 JSON 帧静默忽略
            if not isinstance(data, dict):
                continue

            try:
                frame = ThreadStatusC2SAdapter.validate_python(data)
            except ValidationError:
                continue

            if frame.frame_type == "ping":
                pong = PongFrame(timestamp_ms=_now_ms(), ts=frame.ts)
                await manager.send_to(websocket, pong.model_dump())
            elif frame.frame_type == "approval.inbox.resolve":
                # 用户决策统一路由到 ApprovalManager.resolve；remember 固定写入
                # pending 创建时冻结的 thread 与 canonical candidate。
                decision: dict[str, Any] = {
                    "allow": frame.allow,
                    "message": frame.message,
                    "remember": frame.remember,
                    "rememberRule": (
                        frame.rememberRule.model_dump() if frame.rememberRule is not None else None
                    ),
                }
                accepted = await inbox.resolve(frame.threadId, frame.requestId, decision)
                resolve_result = ApprovalInboxResolveResultFrame(
                    requestId=frame.requestId,
                    accepted=accepted,
                    message=(None if accepted else "规则保存失败、审批已结束或请求不匹配，请重试"),
                )
                await manager.send_to(websocket, resolve_result.model_dump())
            # 其他 frame_type 静默
    except Exception as exc:
        logger.debug("/ws/thread-status client disconnected or errored")
        log_network_event(
            "hosts.web.websocket.thread_status",
            "ws_loop_terminated",
            level="INFO" if isinstance(exc, WebSocketDisconnect) else "WARNING",
            message=str(exc),
        )
    finally:
        # 4. detach 两个 broadcaster
        await inbox.detach(inbox_sender)
        await manager.detach(websocket)


__all__ = [
    "ThreadStatusEventSink",
    "get_thread_status_manager",
    "publish_normalized_status",
    "register_thread_status_routes",
    "reset_broadcaster_for_testing",
]
