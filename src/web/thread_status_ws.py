"""web — thread-status 全局 WS 广播器 + ``/ws/thread-status`` 端点。

设计目的：让所有打开 web app 的客户端实时感知每个 thread 的运行阶段
（idle / responding / thinking / tool_calling / waiting_approval / complete / error），
用于 thread 列表的状态标签渲染。

关键设计（1:1 复用 cron_ws.py 的 Lock + set + attach/detach/broadcast 模式）：

- 独立端点 ``/ws/thread-status``，**不复用** thread WS
- 鉴权：复用 thread WS 的 cookie-based session
- 连接管理：单例 :class:`ThreadStatusBroadcaster` 维护 ``set[WebSocket]``
- broadcast：``asyncio.gather(..., return_exceptions=True)`` 包裹
- ``emit(thread_id, normalized)``：从 normalized["frame_type"] 映射到 phase，
  构造帧后调 ``broadcast``
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Literal, cast

from fastapi import FastAPI, WebSocket

from web.auth import SESSION_COOKIE_NAME, verify_session_cookie

logger = logging.getLogger(__name__)

WS_CLOSE_POLICY_VIOLATION = 1008


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
    "turn.end": "complete",
    "error": "error",
}


class ThreadStatusBroadcaster:
    """thread-status 全局 WS 广播器。

    结构与 :class:`web.cron_ws.CronWSBroker` 完全一致，扩展了
    :meth:`emit` 方法做 kind→phase 映射。
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        """当前连接数（仅供测试 / 监控用，不加锁）。"""
        return len(self._connections)

    async def attach(self, ws: WebSocket) -> None:
        """把 WS 连接加入广播池。重复 attach 同一连接幂等。"""
        async with self._lock:
            self._connections.add(ws)

    async def detach(self, ws: WebSocket) -> None:
        """把 WS 连接移出广播池。不存在的连接忽略。"""
        async with self._lock:
            self._connections.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """异步广播 payload 给所有当前订阅者。"""
        async with self._lock:
            connections = list(self._connections)

        if not connections:
            return

        await asyncio.gather(
            *(self._send_one(ws, payload) for ws in connections),
            return_exceptions=True,
        )

    async def _send_one(self, ws: WebSocket, payload: dict[str, Any]) -> None:
        """对单个连接 send；失败则自动 detach。"""
        try:
            await ws.send_json(payload)
        except Exception:
            await self.detach(ws)

    async def emit(self, thread_id: str, normalized: dict[str, Any]) -> None:
        """从 normalized 消息推导 phase 并广播 thread-status 帧。

        映射规则：

        - ``stream_status`` → 透传 ``normalized["phase"]``
          （仅 responding / thinking / tool_calling）
        - ``permission_request`` → ``waiting_approval``
        - ``permission_cancelled`` → ``idle``
        - ``complete`` → ``complete``
        - ``error`` → ``error``
        - 其他 frame_type → 不推送
        """
        # NormalizedMessage 的判别字段已由 protocol-frame-type-unify-v0.1
        # 从 ``kind`` 改成 ``frame_type``；本路径属内部消费 NormalizedMessage，
        # 跟随真源字段名同步，与 thread-status 出站 wire 帧（``type``）解耦
        frame_type = normalized.get("frame_type")
        if frame_type is None:
            return

        phase: Phase | None = None

        if frame_type == "stream_status":
            raw_phase = normalized.get("phase")
            if isinstance(raw_phase, str) and raw_phase in _STREAM_STATUS_PHASES:
                phase = cast(Phase, raw_phase)
            else:
                return
        else:
            phase = _KIND_TO_PHASE.get(frame_type)

        if phase is None:
            return

        # protocol-frame-type-unify-v0.2：出站帧判别字段从 ``type`` 切到
        # ``frame_type``（与全局 wire 协议对齐）。字面值 ``thread-status``
        # 保持，前端 selector 也按 ``frame_type === "thread-status"`` 切换。
        frame: dict[str, Any] = {
            "frame_type": "thread-status",
            "threadId": thread_id,
            "phase": phase,
        }
        if phase == "tool_calling":
            tool_name = normalized.get("toolName")
            if tool_name is not None:
                frame["toolName"] = tool_name

        await self.broadcast(frame)


class ThreadStatusEventSink:
    """generic_chat 路径的 EventSink 实现——把 Runner 事件映射到 thread-status phase。

    只在 phase 变化时广播，避免 ``content.delta`` / ``reasoning.delta``
    每帧都发（高频事件节流）。
    """

    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id
        self._broadcaster = get_broadcaster()
        self._last_phase: Phase | None = None

    async def emit(self, event: Any) -> None:
        """满足 ``core.contracts.EventSink`` Protocol。"""
        kind = getattr(event, "kind", None)
        if not isinstance(kind, str):
            return

        phase = _EVENT_KIND_TO_PHASE.get(kind)
        if phase is None or phase == self._last_phase:
            return
        self._last_phase = phase

        frame: dict[str, Any] = {
            "frame_type": "thread-status",
            "threadId": self._thread_id,
            "phase": phase,
        }
        if phase == "tool_calling":
            payload = getattr(event, "payload", None) or {}
            tool_name = payload.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                frame["toolName"] = tool_name

        await self._broadcaster.broadcast(frame)


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------

_broadcaster_singleton: ThreadStatusBroadcaster | None = None


def get_broadcaster() -> ThreadStatusBroadcaster:
    """获取或创建 :class:`ThreadStatusBroadcaster` 单例。"""
    global _broadcaster_singleton
    if _broadcaster_singleton is None:
        _broadcaster_singleton = ThreadStatusBroadcaster()
    return _broadcaster_singleton


def reset_broadcaster_for_testing() -> None:
    """**仅测试用**：重置单例到 None。"""
    global _broadcaster_singleton
    _broadcaster_singleton = None


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
    - ``approval.inbox.resolve`` → 路由到 ApprovalInboxBroadcaster.resolve → bridge
    - 其他 → 静默丢弃
    """
    # 延迟 import 避免循环依赖（thread_status_ws 是基础模块，被多处依赖）
    from web.global_approvals import get_inbox_broadcaster

    # 1. cookie 鉴权
    serializer = getattr(websocket.app.state, "serializer", None)
    if serializer is None:
        await websocket.close(
            code=WS_CLOSE_POLICY_VIOLATION,
            reason="auth not configured",
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
    broadcaster = get_broadcaster()
    inbox = get_inbox_broadcaster()
    await broadcaster.attach(websocket)
    await inbox.attach(websocket)

    # 2.5 连接建立时主动 push inbox snapshot（让新连接看到当前所有 pending 审批）
    await inbox.push_snapshot(websocket)

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

            # protocol-frame-type-unify-v0.2：入站判别字段从 ``kind`` 切到
            # ``frame_type``。
            frame_type = data.get("frame_type")
            if frame_type == "ping":
                pong = {
                    "frame_type": "pong",
                    "timestamp_ms": _now_ms(),
                    "ts": data.get("ts"),
                }
                await websocket.send_json(pong)
            elif frame_type == "approval.inbox.resolve":
                # smart-approval-v2-inbox: 用户点三按钮 → 路由回正确 bridge
                thread_id = data.get("threadId")
                request_id = data.get("requestId")
                if not isinstance(thread_id, str) or not isinstance(request_id, str):
                    continue
                # rememberScope（generic-chat-session-grant）：仅在非 None 时
                # 加入 dict，避免污染 claude_code v2-inbox 老 bridge 路径的
                # decision 字面值（老 bridge 完全不感知此字段；下游测试用
                # assert_called_once_with 严格匹配 dict 形状，多余字段会挂）。
                # 用于 ``ApprovalManager.resolve`` 检测 == "session" 时调
                # ``ApprovalRules.add_session_grant`` 写 thread 级 overrides。
                decision: dict[str, Any] = {
                    "allow": bool(data.get("allow", False)),
                    "message": data.get("message"),
                    "rememberEntry": data.get("rememberEntry"),
                }
                remember_scope = data.get("rememberScope")
                if remember_scope is not None:
                    decision["rememberScope"] = remember_scope
                inbox.resolve(thread_id, request_id, decision)
            # 其他 frame_type 静默
    except Exception:
        logger.debug("/ws/thread-status client disconnected or errored")
    finally:
        # 4. detach 两个 broadcaster
        await broadcaster.detach(websocket)
        await inbox.detach(websocket)


__all__ = [
    "ThreadStatusBroadcaster",
    "ThreadStatusEventSink",
    "get_broadcaster",
    "register_thread_status_routes",
    "reset_broadcaster_for_testing",
]
