"""场景 3 + 4：resolve 路由 + abort/cancel → remove(reason=cancelled)。

场景 3：前端通过 ws 发 ``approval.inbox.resolve`` → 后端按 threadId 在
``_bridge_registry`` 找到 bridge → 调 ``bridge.resolve(request_id, decision)``

场景 4：``ApprovalBridge.can_use_tool`` 内 wait_for 被 cancel → 自动
emit_remove(reason="cancelled") → 所有订阅者收到 ``approval.inbox.remove`` 帧
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk.types import ToolPermissionContext
from fastapi.testclient import TestClient

from web._shared.session_manager import SessionManager
from web.approvals.global_inbox.broadcaster import get_inbox_broadcaster
from web.integrations.claude_code.approval import ApprovalBridge
from web.integrations.claude_code.normalizer import ClaudeNormalizer


def _drain_snapshot(ws: Any) -> dict[str, Any]:
    frame = ws.receive_json()
    assert frame["frame_type"] == "approval.inbox.snapshot"
    return frame


class _FakeWriter:
    """ApprovalBridge.active_writer 接口：仅需 async send_json。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)


def _make_ctx(tool_use_id: str = "toolu_test") -> ToolPermissionContext:
    return ToolPermissionContext(
        signal=None,
        suggestions=[],
        tool_use_id=tool_use_id,
        agent_id=None,
    )


# ---------------------------------------------------------------------------
# 场景 3：resolve 路由
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbox_resolve_routes_to_correct_bridge_by_thread_id(
    authed_client: TestClient,
) -> None:
    """前端发 approval.inbox.resolve {threadId, requestId, allow} → 按 threadId 路由 bridge.resolve。"""
    broadcaster = get_inbox_broadcaster()

    # 用 MagicMock 替代真 bridge，断言 resolve 被调
    bridge_a = MagicMock()
    bridge_a.resolve = MagicMock(return_value=True)
    bridge_b = MagicMock()
    bridge_b.resolve = MagicMock(return_value=True)
    broadcaster.register_bridge("thread-aaa", bridge_a)
    broadcaster.register_bridge("thread-bbb", bridge_b)

    with authed_client.websocket_connect("/ws/thread-status") as ws:
        _drain_snapshot(ws)

        # 前端 ack thread-aaa 的某条审批
        ws.send_json(
            {
                "frame_type": "approval.inbox.resolve",
                "threadId": "thread-aaa",
                "requestId": "toolu_x",
                "allow": True,
                "message": None,
                "rememberEntry": None,
            }
        )
        # ws.send 是同步入队，给 portal 一点时间消费这帧
        for _ in range(50):
            if bridge_a.resolve.called:
                break
            await asyncio.sleep(0.02)

    # bridge_a 被路由到，bridge_b 没被调
    bridge_a.resolve.assert_called_once_with(
        "toolu_x",
        {"allow": True, "message": None, "rememberEntry": None},
    )
    bridge_b.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_inbox_resolve_unknown_thread_silently_dropped(
    authed_client: TestClient,
) -> None:
    """threadId 在 registry 不存在 → broadcaster.resolve 返回 False，连接不挂。"""
    with authed_client.websocket_connect("/ws/thread-status") as ws:
        _drain_snapshot(ws)
        ws.send_json(
            {
                "frame_type": "approval.inbox.resolve",
                "threadId": "thread-never-existed",
                "requestId": "toolu_x",
                "allow": False,
            }
        )
        # 不应收到任何回帧（handler 静默丢弃）；用 ping 验证连接还活着
        ws.send_json({"frame_type": "ping"})
        pong = ws.receive_json()
        assert pong["frame_type"] == "pong"


@pytest.mark.asyncio
async def test_inbox_resolve_missing_thread_id_silently_dropped(
    authed_client: TestClient,
) -> None:
    """缺 threadId 字段 → handler 校验失败静默丢；不调任何 bridge。"""
    broadcaster = get_inbox_broadcaster()
    bridge = MagicMock()
    bridge.resolve = MagicMock(return_value=True)
    broadcaster.register_bridge("thread-aaa", bridge)

    with authed_client.websocket_connect("/ws/thread-status") as ws:
        _drain_snapshot(ws)
        ws.send_json(
            {
                "frame_type": "approval.inbox.resolve",
                # 故意缺 threadId
                "requestId": "toolu_x",
                "allow": True,
            }
        )
        # 用 ping 探活
        ws.send_json({"frame_type": "ping"})
        pong = ws.receive_json()
        assert pong["frame_type"] == "pong"

    bridge.resolve.assert_not_called()


# ---------------------------------------------------------------------------
# 场景 4：abort/cancel → emit_remove(reason="cancelled")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_can_use_tool_cancelled_emits_remove_cancelled(
    authed_client: TestClient,
) -> None:
    """ApprovalBridge.can_use_tool 跑到 wait_for → task.cancel() →
    finally 路径调 _inbox_emit_remove_safe(rid, "cancelled") →
    订阅者收到 approval.inbox.remove reason=cancelled。
    """
    broadcaster = get_inbox_broadcaster()

    # 构造真 ApprovalBridge，注入 broadcaster
    bridge = ApprovalBridge(
        ClaudeNormalizer(),
        SessionManager(),
        thread_id="thread-cancel",
        inbox_broadcaster=broadcaster,
    )
    writer = _FakeWriter()
    bridge.set_active_writer(writer)

    with authed_client.websocket_connect("/ws/thread-status") as ws:
        _drain_snapshot(ws)

        # 启动 can_use_tool 作为后台任务（policy=None 走老路径 = 无限 await future）
        task = asyncio.create_task(
            bridge.can_use_tool("Read", {"path": "/tmp/x"}, _make_ctx("toolu_cancel"))
        )

        # 等 can_use_tool 走到 emit_add 并挂上 Future
        for _ in range(50):
            if broadcaster.pending_count >= 1 and len(writer.sent) >= 1:
                break
            await asyncio.sleep(0.02)
        assert broadcaster.pending_count == 1

        # 排出 add 帧（fan-out 到 ws）
        add_frame = ws.receive_json()
        assert add_frame["frame_type"] == "approval.inbox.add"
        assert add_frame["requestId"] == "toolu_cancel"
        assert add_frame["threadId"] == "thread-cancel"

        # 触发 abort/cancel：task.cancel() → except asyncio.CancelledError 路径 →
        # _inbox_emit_remove_safe(rid, "cancelled")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 收到 remove 帧 reason=cancelled
        remove_frame = ws.receive_json()
        assert remove_frame["frame_type"] == "approval.inbox.remove"
        assert remove_frame["requestId"] == "toolu_cancel"
        assert remove_frame["reason"] == "cancelled"
        # snapshot 也清掉
        assert broadcaster.pending_count == 0


@pytest.mark.asyncio
async def test_bridge_resolve_emits_remove_user_decided(
    authed_client: TestClient,
) -> None:
    """对照组：bridge.resolve(allow=True) → 正常退出 → remove reason=user_decided。"""
    broadcaster = get_inbox_broadcaster()

    bridge = ApprovalBridge(
        ClaudeNormalizer(),
        SessionManager(),
        thread_id="thread-decide",
        inbox_broadcaster=broadcaster,
    )
    writer = _FakeWriter()
    bridge.set_active_writer(writer)

    with authed_client.websocket_connect("/ws/thread-status") as ws:
        _drain_snapshot(ws)

        task = asyncio.create_task(
            bridge.can_use_tool("Read", {"path": "/tmp/y"}, _make_ctx("toolu_decide"))
        )

        for _ in range(50):
            if broadcaster.pending_count >= 1 and len(writer.sent) >= 1:
                break
            await asyncio.sleep(0.02)
        assert broadcaster.pending_count == 1

        add_frame = ws.receive_json()
        assert add_frame["frame_type"] == "approval.inbox.add"

        # 模拟用户决策
        bridge.resolve("toolu_decide", {"allow": True})

        result = await task
        # 不深入断言 result 形态，避免耦合；只校验 remove 路径
        assert result is not None

        remove_frame = ws.receive_json()
        assert remove_frame["frame_type"] == "approval.inbox.remove"
        assert remove_frame["requestId"] == "toolu_decide"
        assert remove_frame["reason"] == "user_decided"
