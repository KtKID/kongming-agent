"""场景 2：snapshot 重连补包。

先在 broadcaster 上 emit_add 几条 → 新 ws attach → 必须立刻收到
``approval.inbox.snapshot``，items 包含所有 pending 审批。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from web.approvals.global_inbox.broadcaster import get_inbox_broadcaster


def _make_payload(request_id: str, thread_id: str = "thread-aaa") -> dict[str, Any]:
    return {
        "requestId": request_id,
        "threadId": thread_id,
        "toolName": "Read",
        "toolInput": {"path": "/tmp/x"},
        "autoApproveAtMs": 1_000_000_999,
        "autoRejectAtMs": None,
        "blockedByRule": None,
        "isElevated": False,
        "channel": "claude_code",
        "cwd": "/proj/x",
        "arrivedAtMs": 1_000_000_000,
    }


@pytest.mark.asyncio
async def test_new_subscriber_receives_snapshot_with_all_pending(
    authed_client: TestClient,
) -> None:
    """先 emit 3 条 pending（无订阅者），新 ws 连上立刻收到含 3 条的 snapshot。"""
    broadcaster = get_inbox_broadcaster()

    # 无订阅者时 emit_add：snapshot 累积，无 fan-out 接收
    await broadcaster.emit_add(_make_payload("toolu_a", "thread-aaa"))
    await broadcaster.emit_add(_make_payload("toolu_b", "thread-bbb"))
    await broadcaster.emit_add(_make_payload("toolu_c", "thread-ccc"))
    assert broadcaster.pending_count == 3

    # 新 ws 连上 → handler 立刻调 push_snapshot
    with authed_client.websocket_connect("/ws/thread-status") as ws:
        snapshot = ws.receive_json()
        assert snapshot["frame_type"] == "approval.inbox.snapshot"
        items = snapshot["items"]
        assert len(items) == 3
        ids = sorted(item["requestId"] for item in items)
        assert ids == ["toolu_a", "toolu_b", "toolu_c"]
        # 完整 payload 字段检查（其中一条）
        a = next(item for item in items if item["requestId"] == "toolu_a")
        assert a["threadId"] == "thread-aaa"
        assert a["toolName"] == "Read"
        assert a["toolInput"] == {"path": "/tmp/x"}
        assert a["autoApproveAtMs"] == 1_000_000_999
        assert a["channel"] == "claude_code"


@pytest.mark.asyncio
async def test_snapshot_empty_when_no_pending(
    authed_client: TestClient,
) -> None:
    """无 pending 时 snapshot.items 为空列表。"""
    with authed_client.websocket_connect("/ws/thread-status") as ws:
        snapshot = ws.receive_json()
        assert snapshot == {"frame_type": "approval.inbox.snapshot", "items": []}


@pytest.mark.asyncio
async def test_snapshot_reflects_removed_pending(
    authed_client: TestClient,
) -> None:
    """add → remove 后 snapshot 不再含已 remove 的条目。"""
    broadcaster = get_inbox_broadcaster()
    await broadcaster.emit_add(_make_payload("toolu_a"))
    await broadcaster.emit_add(_make_payload("toolu_b"))
    await broadcaster.emit_remove("toolu_a", reason="user_decided")
    assert broadcaster.pending_count == 1

    with authed_client.websocket_connect("/ws/thread-status") as ws:
        snapshot = ws.receive_json()
        assert snapshot["frame_type"] == "approval.inbox.snapshot"
        ids = [item["requestId"] for item in snapshot["items"]]
        assert ids == ["toolu_b"]
