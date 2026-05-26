"""``network.heartbeat`` 单元测试。

覆盖：

1. ping 帧 → 返回 pong 含 echo 的 ts + 服务端 timestamp_ms
2. 非 ping 帧（业务 type / pong / 其他 frame_type）→ 返回 None
3. 两个 Heartbeat 实例并存互不串扰（多频道扩展性铁律）
"""

from __future__ import annotations

from typing import Any

import pytest

from network import Heartbeat, HeartbeatConfig, HeartbeatHooks


class _DummyHooks(HeartbeatHooks):
    """最小 hooks 实现；v0.1 服务端被动响应不调用 hooks，仅满足 Protocol。"""

    def __init__(self) -> None:
        self.sent_pongs: list[dict[str, Any]] = []

    def is_open(self) -> bool:
        return True

    async def send_pong(self, frame: dict[str, Any]) -> None:
        self.sent_pongs.append(frame)


def _make_config() -> HeartbeatConfig:
    return HeartbeatConfig(interval_ms=30000, timeout_ms=10000, max_missed=3)


@pytest.mark.asyncio
async def test_handle_inbound_frame_returns_pong_for_ping_with_echoed_ts() -> None:
    """ping 帧 → pong 帧，``ts`` 原样回传，``timestamp_ms`` 为服务端当前时间。"""
    heartbeat = Heartbeat(_make_config(), _DummyHooks())
    frame = {"frame_type": "ping", "ts": 1234567890}
    response = await heartbeat.handle_inbound_frame(frame)
    assert response is not None
    assert response["frame_type"] == "pong"
    assert response["ts"] == 1234567890
    # timestamp_ms 是 int 毫秒；不写死值，只校验类型 + 大致量级
    assert isinstance(response["timestamp_ms"], int)
    assert response["timestamp_ms"] > 0


@pytest.mark.asyncio
async def test_handle_inbound_frame_handles_ping_without_ts() -> None:
    """ping 帧不带 ts → pong 帧 ts=None（不抛 KeyError）。"""
    heartbeat = Heartbeat(_make_config(), _DummyHooks())
    response = await heartbeat.handle_inbound_frame({"frame_type": "ping"})
    assert response is not None
    assert response["frame_type"] == "pong"
    assert response["ts"] is None
    assert isinstance(response["timestamp_ms"], int)


@pytest.mark.asyncio
async def test_handle_inbound_frame_returns_none_for_non_ping_frame() -> None:
    """业务帧（type / 其他 frame_type / 不是 dict）→ 返回 None 让上层路由。"""
    heartbeat = Heartbeat(_make_config(), _DummyHooks())
    # 业务 type 字段（Claude 频道 6 个 msg_type）
    assert await heartbeat.handle_inbound_frame({"type": "claude-command"}) is None
    # 其他 frame_type（应回的 pong 不应该走到这条路径，但万一收到也不应触发）
    assert await heartbeat.handle_inbound_frame({"frame_type": "pong"}) is None
    # 完全无 frame_type / type 字段
    assert await heartbeat.handle_inbound_frame({}) is None


@pytest.mark.asyncio
async def test_two_heartbeat_instances_do_not_share_state() -> None:
    """两个 Heartbeat 实例并存：调用其一不影响其二（多频道并发扩展性铁律）。

    本测试是 spec 02 ``Heartbeat 设计`` 节强调的 ``多实例并发安全`` 核心
    验收。v0.1 服务端 Heartbeat 实现为纯函数式，无可变共享状态，但仍
    需要单测固化以防 v0.3 升级到 server-push ping 时不小心引入类 / 模块
    级共享变量。
    """
    hooks_a = _DummyHooks()
    hooks_b = _DummyHooks()
    hb_a = Heartbeat(_make_config(), hooks_a)
    hb_b = Heartbeat(_make_config(), hooks_b)

    # 仅对 A 处理 ping
    resp_a = await hb_a.handle_inbound_frame({"frame_type": "ping", "ts": 1000})
    assert resp_a is not None
    assert resp_a["ts"] == 1000

    # B 没被调过任何方法 → latency 仍 None
    assert hb_b.latency_ms is None
    # B 自己的 hooks 没收到任何 pong
    assert hooks_b.sent_pongs == []

    # 对 B 调一次不同 ts
    resp_b = await hb_b.handle_inbound_frame({"frame_type": "ping", "ts": 9999})
    assert resp_b is not None
    assert resp_b["ts"] == 9999
    # A 状态不受影响
    assert hb_a.latency_ms is None
