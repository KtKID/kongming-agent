"""ApprovalInboxBroadcaster 单测。

覆盖：
- attach/detach + 重复 attach 幂等
- emit_add 写入 snapshot + fan-out
- emit_remove 清 snapshot + fan-out
- push_snapshot 给单个 ws
- resolve target register/unregister/resolve
- 并发 emit / detach 不死锁
- 失败连接自动 detach（不影响其他订阅者）
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hosts.web.approvals.global_inbox.broadcaster import (
    ApprovalInboxBroadcaster,
    get_inbox_broadcaster,
    reset_inbox_broadcaster_for_testing,
)


class _FakeWS:
    """模仿 FastAPI WebSocket，记录所有 send_json 的帧。"""

    def __init__(self, raise_on_send: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self._raise = raise_on_send

    async def send_json(self, frame: dict[str, Any]) -> None:
        if self._raise:
            raise ConnectionError("simulated disconnect")
        self.sent.append(frame)


class _BlockingWS(_FakeWS):
    """首帧发送可控阻塞，用于证明 snapshot 与增量共享同一排队边界。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send_json(self, frame: dict[str, Any]) -> None:
        """首个 send 停在闸门，后续 send 按调用顺序直接记录。"""
        if not self.sent:
            self.entered.set()
            await self.release.wait()
        self.sent.append(frame)


def _make_payload(
    request_id: str = "toolu_x",
    thread_id: str = "thread-aaa",
) -> dict[str, Any]:
    return {
        "requestId": request_id,
        "threadId": thread_id,
        "toolName": "Bash",
        "toolInput": {"command": "ls"},
        "blockedByRule": None,
        "isElevated": False,
        "danger": False,
        "rememberAllowed": False,
        "channel": "claude_code",
        "cwd": "/proj/x",
        "arrivedAtMs": 1_000_000_000,
    }


@pytest.fixture()
def broadcaster() -> ApprovalInboxBroadcaster:
    return ApprovalInboxBroadcaster()


# ---------- 订阅者管理 ----------


class TestAttachDetach:
    async def test_attach_increments_count(self, broadcaster: ApprovalInboxBroadcaster) -> None:
        ws = _FakeWS()
        await broadcaster.attach(ws)
        assert broadcaster.subscriber_count == 1

    async def test_attach_idempotent(self, broadcaster: ApprovalInboxBroadcaster) -> None:
        ws = _FakeWS()
        await broadcaster.attach(ws)
        await broadcaster.attach(ws)
        assert broadcaster.subscriber_count == 1

    async def test_detach_removes(self, broadcaster: ApprovalInboxBroadcaster) -> None:
        ws = _FakeWS()
        await broadcaster.attach(ws)
        await broadcaster.detach(ws)
        assert broadcaster.subscriber_count == 0

    async def test_detach_unknown_silent(self, broadcaster: ApprovalInboxBroadcaster) -> None:
        await broadcaster.detach(_FakeWS())  # 不抛
        assert broadcaster.subscriber_count == 0


# ---------- emit_add / emit_remove / snapshot ----------


class TestEmit:
    async def test_emit_add_fans_out_to_all(self, broadcaster: ApprovalInboxBroadcaster) -> None:
        ws1, ws2, ws3 = _FakeWS(), _FakeWS(), _FakeWS()
        await broadcaster.attach(ws1)
        await broadcaster.attach(ws2)
        await broadcaster.attach(ws3)

        payload = _make_payload(request_id="toolu_a")
        await broadcaster.emit_add(payload)

        for ws in (ws1, ws2, ws3):
            assert len(ws.sent) == 1
            assert ws.sent[0]["frame_type"] == "approval.inbox.add"
            assert ws.sent[0]["requestId"] == "toolu_a"
            assert ws.sent[0]["threadId"] == "thread-aaa"

    async def test_emit_add_writes_snapshot(self, broadcaster: ApprovalInboxBroadcaster) -> None:
        await broadcaster.emit_add(_make_payload(request_id="toolu_a"))
        await broadcaster.emit_add(_make_payload(request_id="toolu_b"))
        assert broadcaster.pending_count == 2

    async def test_emit_add_skip_missing_request_id(
        self, broadcaster: ApprovalInboxBroadcaster
    ) -> None:
        ws = _FakeWS()
        await broadcaster.attach(ws)
        bad = _make_payload()
        del bad["requestId"]
        await broadcaster.emit_add(bad)
        assert ws.sent == []
        assert broadcaster.pending_count == 0

    async def test_emit_remove_clears_snapshot(self, broadcaster: ApprovalInboxBroadcaster) -> None:
        await broadcaster.emit_add(_make_payload(request_id="toolu_a"))
        await broadcaster.emit_remove("toolu_a", reason="user_decided")
        assert broadcaster.pending_count == 0

    async def test_emit_remove_fans_out(self, broadcaster: ApprovalInboxBroadcaster) -> None:
        ws = _FakeWS()
        await broadcaster.attach(ws)
        await broadcaster.emit_add(_make_payload(request_id="toolu_a"))
        await broadcaster.emit_remove("toolu_a", reason="timeout")
        # ws.sent[0] = add, [1] = remove
        assert len(ws.sent) == 2
        assert ws.sent[1] == {
            "frame_type": "approval.inbox.remove",
            "requestId": "toolu_a",
            "reason": "timeout",
        }

    async def test_emit_remove_unknown_request_silent(
        self, broadcaster: ApprovalInboxBroadcaster
    ) -> None:
        ws = _FakeWS()
        await broadcaster.attach(ws)
        await broadcaster.emit_remove("unknown", reason="cancelled")
        # 仍 fan-out（前端可幂等忽略）；snapshot 没动
        assert len(ws.sent) == 1
        assert broadcaster.pending_count == 0

    async def test_emit_no_subscribers_silent(self, broadcaster: ApprovalInboxBroadcaster) -> None:
        # 没订阅者时 emit 不抛
        await broadcaster.emit_add(_make_payload())
        await broadcaster.emit_remove("toolu_x", reason="cancelled")
        assert broadcaster.pending_count == 0


class TestPushSnapshot:
    async def test_push_snapshot_to_new_subscriber(
        self, broadcaster: ApprovalInboxBroadcaster
    ) -> None:
        # 先有 2 个 pending
        await broadcaster.emit_add(_make_payload(request_id="a"))
        await broadcaster.emit_add(_make_payload(request_id="b"))

        # 新 ws 连上来，push snapshot
        ws = _FakeWS()
        await broadcaster.attach(ws)
        await broadcaster.push_snapshot(ws)

        assert len(ws.sent) == 1
        frame = ws.sent[0]
        assert frame["frame_type"] == "approval.inbox.snapshot"
        ids = sorted(item["requestId"] for item in frame["items"])
        assert ids == ["a", "b"]

    async def test_push_snapshot_empty(self, broadcaster: ApprovalInboxBroadcaster) -> None:
        ws = _FakeWS()
        await broadcaster.attach(ws)
        await broadcaster.push_snapshot(ws)
        assert ws.sent[0] == {"frame_type": "approval.inbox.snapshot", "items": []}


# ---------- 失败连接自动 detach ----------


class TestBrokenConnection:
    async def test_failing_ws_detached_on_broadcast(
        self, broadcaster: ApprovalInboxBroadcaster
    ) -> None:
        ok = _FakeWS()
        bad = _FakeWS(raise_on_send=True)
        await broadcaster.attach(ok)
        await broadcaster.attach(bad)

        await broadcaster.emit_add(_make_payload(request_id="x"))

        # bad 已被 detach；ok 仍正常收到
        assert broadcaster.subscriber_count == 1
        assert len(ok.sent) == 1

    async def test_failing_ws_detached_on_push_snapshot(
        self, broadcaster: ApprovalInboxBroadcaster
    ) -> None:
        bad = _FakeWS(raise_on_send=True)
        await broadcaster.attach(bad)
        await broadcaster.push_snapshot(bad)
        assert broadcaster.subscriber_count == 0


# ---------- 并发 ----------


class TestConcurrency:
    async def test_concurrent_emit_and_attach(self, broadcaster: ApprovalInboxBroadcaster) -> None:
        """20 个并发 emit_add + 10 个并发 attach 不死锁；最终所有订阅者都收到末次 emit。"""
        wses = [_FakeWS() for _ in range(10)]
        await asyncio.gather(*(broadcaster.attach(w) for w in wses))

        await asyncio.gather(
            *(broadcaster.emit_add(_make_payload(request_id=f"toolu_{i}")) for i in range(20))
        )

        assert broadcaster.pending_count == 20
        for w in wses:
            # 每个 ws 应收到 20 个 add 帧（顺序可能不同）
            assert len(w.sent) == 20

    async def test_concurrent_emit_and_remove(self, broadcaster: ApprovalInboxBroadcaster) -> None:
        """emit_add 后并发 emit_remove，最终 snapshot 清空。"""
        ws = _FakeWS()
        await broadcaster.attach(ws)
        request_ids = [f"toolu_{i}" for i in range(30)]
        await asyncio.gather(
            *(broadcaster.emit_add(_make_payload(request_id=r)) for r in request_ids)
        )
        await asyncio.gather(
            *(broadcaster.emit_remove(r, reason="user_decided") for r in request_ids)
        )
        assert broadcaster.pending_count == 0

    async def test_snapshot_enqueue_precedes_concurrent_add(
        self,
        broadcaster: ApprovalInboxBroadcaster,
    ) -> None:
        """旧空 snapshot 必须先于并发 add，避免前端清掉刚到的新卡。"""
        ws = _BlockingWS()
        await broadcaster.attach(ws)
        snapshot_task = asyncio.create_task(broadcaster.push_snapshot(ws))
        await ws.entered.wait()

        add_task = asyncio.create_task(
            broadcaster.emit_add(_make_payload(request_id="new-request"))
        )
        await asyncio.sleep(0)
        assert add_task.done() is False

        ws.release.set()
        await asyncio.gather(snapshot_task, add_task)
        assert [frame["frame_type"] for frame in ws.sent] == [
            "approval.inbox.snapshot",
            "approval.inbox.add",
        ]

    async def test_snapshot_enqueue_precedes_concurrent_remove(
        self,
        broadcaster: ApprovalInboxBroadcaster,
    ) -> None:
        """含旧卡 snapshot 必须先于并发 remove，避免前端复活已结束卡片。"""
        await broadcaster.emit_add(_make_payload(request_id="old-request"))
        ws = _BlockingWS()
        await broadcaster.attach(ws)
        snapshot_task = asyncio.create_task(broadcaster.push_snapshot(ws))
        await ws.entered.wait()

        remove_task = asyncio.create_task(
            broadcaster.emit_remove("old-request", reason="user_decided")
        )
        await asyncio.sleep(0)
        assert remove_task.done() is False

        ws.release.set()
        await asyncio.gather(snapshot_task, remove_task)
        assert [frame["frame_type"] for frame in ws.sent] == [
            "approval.inbox.snapshot",
            "approval.inbox.remove",
        ]


# ---------- 单例 ----------


class TestSingleton:
    def test_get_returns_same_instance(self) -> None:
        reset_inbox_broadcaster_for_testing()
        a = get_inbox_broadcaster()
        b = get_inbox_broadcaster()
        assert a is b

    def test_reset_creates_new(self) -> None:
        reset_inbox_broadcaster_for_testing()
        a = get_inbox_broadcaster()
        reset_inbox_broadcaster_for_testing()
        b = get_inbox_broadcaster()
        assert a is not b


# ---------- resolve_target registry（阶段 1 新增） ----------


class TestRegisterResolveTarget:
    """阶段 1 新增：register_resolve_target / unregister_resolve_target API。"""

    async def test_register_then_resolve_routes_to_callback(self) -> None:
        """register 后 resolve(thread_id, req_id, ...) 调用对应 callback。"""
        bc = ApprovalInboxBroadcaster()
        called: list[tuple[str, str, dict[str, Any]]] = []

        async def cb(thread_id: str, req_id: str, dec: dict[str, Any]) -> bool:
            called.append((thread_id, req_id, dec))
            return True

        bc.register_resolve_target("t1", cb)
        assert await bc.resolve("t1", "r1", {"allow": True}) is True
        assert called == [("t1", "r1", {"allow": True})]

    def test_unregister_with_is_check_does_not_delete_overridden(self) -> None:
        """老 callback unregister 不能删新 callback（is 比较防覆盖误删）。"""
        bc = ApprovalInboxBroadcaster()

        async def cb_old(thread_id: str, req_id: str, dec: dict[str, Any]) -> bool:
            return True

        async def cb_new(thread_id: str, req_id: str, dec: dict[str, Any]) -> bool:
            return True

        bc.register_resolve_target("t1", cb_old)
        bc.register_resolve_target("t1", cb_new)  # 覆盖
        bc.unregister_resolve_target("t1", cb_old)  # 老 callback unregister
        # cb_new 仍在 registry
        assert "t1" in bc._resolve_targets
        assert bc._resolve_targets["t1"] is cb_new

    async def test_resolve_uses_registered_target(self) -> None:
        """resolve 始终路由到统一 ApprovalManager callback。"""
        bc = ApprovalInboxBroadcaster()
        target_called: list[tuple[str, str, dict[str, Any]]] = []

        async def cb(thread_id: str, req_id: str, dec: dict[str, Any]) -> bool:
            target_called.append((thread_id, req_id, dec))
            return True

        bc.register_resolve_target("t1", cb)

        result = await bc.resolve("t1", "r1", {"allow": True})
        assert result is True
        assert target_called == [("t1", "r1", {"allow": True})]

    def test_register_empty_thread_id_silent(self) -> None:
        """空 thread_id 静默忽略（与 register_bridge 一致）。"""
        bc = ApprovalInboxBroadcaster()

        async def cb(thread_id: str, req_id: str, dec: dict[str, Any]) -> bool:
            return True

        bc.register_resolve_target("", cb)
        assert "" not in bc._resolve_targets

    def test_unregister_unknown_silent(self) -> None:
        """没注册过 unregister 不抛。"""
        bc = ApprovalInboxBroadcaster()

        async def cb(thread_id: str, req_id: str, dec: dict[str, Any]) -> bool:
            return True

        bc.unregister_resolve_target("t-never", cb)  # 不抛
