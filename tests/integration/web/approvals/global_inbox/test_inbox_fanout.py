"""场景 1：多 thread / 多订阅者 ws 同时触发审批 → 全员收到 add 帧。

走真实 ``/ws/thread-status`` 端点 + ``ApprovalInboxBroadcaster`` 单例。

注意：TestClient 的 ``websocket_connect`` 同步返回时机是 endpoint 调 accept
之后，**后续 attach / push_snapshot 仍在 portal 线程后台跑**。所以测里用
``_drain_snapshot`` 阻塞接收第一帧 ``approval.inbox.snapshot``——只有 attach
完成才会发 snapshot——保证后续 ``emit_add`` 时所有订阅者都已注册。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from hosts.web.approvals.global_inbox.broadcaster import get_inbox_broadcaster


def _drain_snapshot(ws: Any) -> dict[str, Any]:
    """连上后依次消费 thread status 与 approval inbox 两个 snapshot。"""
    status_frame = ws.receive_json()
    assert status_frame["frame_type"] == "thread-status.snapshot"
    inbox_frame = ws.receive_json()
    assert inbox_frame["frame_type"] == "approval.inbox.snapshot"
    return inbox_frame


def _make_payload(request_id: str, thread_id: str = "thread-aaa") -> dict[str, Any]:
    return {
        "requestId": request_id,
        "threadId": thread_id,
        "toolName": "Bash",
        "toolInput": {"command": "ls"},
        "autoApproveAtMs": None,
        "blockedByRule": None,
        "isElevated": False,
        "danger": False,
        "rememberAllowed": False,
        "channel": "claude_code",
        "cwd": "/proj/x",
        "arrivedAtMs": 1_000_000_000,
    }


@pytest.mark.asyncio
async def test_inbox_add_fans_out_to_all_subscribers(
    authed_client: TestClient,
) -> None:
    """3 个 ws 订阅者：单次 emit_add → 全员收到 approval.inbox.add 帧。"""
    with (
        authed_client.websocket_connect("/ws/thread-status") as ws1,
        authed_client.websocket_connect("/ws/thread-status") as ws2,
        authed_client.websocket_connect("/ws/thread-status") as ws3,
    ):
        # 每条 ws 连上后会先收到 snapshot（空）—— 接收 snapshot 隐含 attach 已完成
        for ws in (ws1, ws2, ws3):
            _drain_snapshot(ws)

        broadcaster = get_inbox_broadcaster()
        assert broadcaster.subscriber_count == 3

        # 模拟两个 thread 触发审批 — 后端 fan-out
        await broadcaster.emit_add(_make_payload("toolu_a", "thread-aaa"))
        await broadcaster.emit_add(_make_payload("toolu_b", "thread-bbb"))

        # 三个 ws 都应该收到这两条 add 帧
        for ws in (ws1, ws2, ws3):
            frame_a = ws.receive_json()
            frame_b = ws.receive_json()
            assert frame_a["frame_type"] == "approval.inbox.add"
            assert frame_a["requestId"] == "toolu_a"
            assert frame_a["threadId"] == "thread-aaa"
            assert frame_a["toolName"] == "Bash"
            assert frame_b["frame_type"] == "approval.inbox.add"
            assert frame_b["requestId"] == "toolu_b"
            assert frame_b["threadId"] == "thread-bbb"


@pytest.mark.asyncio
async def test_inbox_remove_fans_out_to_all_subscribers(
    authed_client: TestClient,
) -> None:
    """emit_remove 同样 fan-out 给所有订阅者。"""
    with (
        authed_client.websocket_connect("/ws/thread-status") as ws1,
        authed_client.websocket_connect("/ws/thread-status") as ws2,
    ):
        _drain_snapshot(ws1)
        _drain_snapshot(ws2)

        broadcaster = get_inbox_broadcaster()
        assert broadcaster.subscriber_count == 2

        await broadcaster.emit_add(_make_payload("toolu_x"))
        await broadcaster.emit_remove("toolu_x", reason="user_decided")

        for ws in (ws1, ws2):
            add_frame = ws.receive_json()
            remove_frame = ws.receive_json()
            assert add_frame["frame_type"] == "approval.inbox.add"
            assert remove_frame["frame_type"] == "approval.inbox.remove"
            assert remove_frame["requestId"] == "toolu_x"
            assert remove_frame["reason"] == "user_decided"
