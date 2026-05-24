"""``network.manager`` 单元测试。

覆盖（dev-checklist 8.6）：

1. register 创建 ConnectionInfo + Heartbeat 对
2. unregister 移除两个 dict
3. handle_inbound 拦下 ping → websocket 收到 pong
4. handle_inbound 业务帧 → 返回 False，websocket 没收到 pong
5. 未 configure 就 register → RuntimeError
6. send 失败时自动 unregister
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from network.manager import (
    HeartbeatConfig,
    NetworkManager,
)


def _make_config() -> HeartbeatConfig:
    return HeartbeatConfig(interval_ms=30000, timeout_ms=10000, max_missed=3)


def _make_ws() -> AsyncMock:
    """AsyncMock 模拟 WebSocket；send_json 是异步方法。"""
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_register_creates_connection_and_heartbeat_pair() -> None:
    """register → conn_id 唯一 + 双 dict 同时建条目 + 元信息正确。"""
    manager = NetworkManager()
    manager.configure(_make_config())
    ws = _make_ws()
    conn_id = await manager.register("claude", ws, thread_id="thread-aaaaaaaaaaaa")

    assert isinstance(conn_id, str)
    assert len(conn_id) == 32  # uuid4().hex

    # 检查 _connections / _heartbeats 双 dict
    assert conn_id in manager._connections
    assert conn_id in manager._heartbeats

    info = manager._connections[conn_id]
    assert info.conn_id == conn_id
    assert info.channel == "claude"
    assert info.thread_id == "thread-aaaaaaaaaaaa"
    assert info.websocket is ws
    assert info.registered_at_ms > 0


@pytest.mark.asyncio
async def test_unregister_removes_both() -> None:
    """unregister 从两个 dict 同时清除；幂等：重复 unregister 不抛。"""
    manager = NetworkManager()
    manager.configure(_make_config())
    ws = _make_ws()
    conn_id = await manager.register("claude", ws)
    assert conn_id in manager._connections

    await manager.unregister(conn_id)
    assert conn_id not in manager._connections
    assert conn_id not in manager._heartbeats

    # 幂等再调一次
    await manager.unregister(conn_id)
    await manager.unregister("nonexistent-conn-id")


@pytest.mark.asyncio
async def test_handle_inbound_routes_ping_returns_pong_via_websocket() -> None:
    """ping 帧被拦下 → 返回 True + websocket.send_json 被调用回 pong。"""
    manager = NetworkManager()
    manager.configure(_make_config())
    ws = _make_ws()
    conn_id = await manager.register("claude", ws)

    handled = await manager.handle_inbound(conn_id, {"kind": "ping", "ts": 12345})

    assert handled is True
    ws.send_json.assert_called_once()
    # 验证 pong 帧内容
    call_args = ws.send_json.call_args
    pong = call_args.args[0]
    assert pong["kind"] == "pong"
    assert pong["ts"] == 12345
    assert isinstance(pong["timestamp_ms"], int)


@pytest.mark.asyncio
async def test_handle_inbound_returns_false_for_business_frame() -> None:
    """业务帧（无 kind / kind != ping）→ 返回 False + ws 未被调用。"""
    manager = NetworkManager()
    manager.configure(_make_config())
    ws = _make_ws()
    conn_id = await manager.register("claude", ws)

    # 业务 msg_type 帧
    handled = await manager.handle_inbound(conn_id, {"type": "claude-command", "command": "hi"})
    assert handled is False
    ws.send_json.assert_not_called()

    # 完全空 dict
    handled = await manager.handle_inbound(conn_id, {})
    assert handled is False
    ws.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_handle_inbound_returns_false_for_unknown_conn_id() -> None:
    """unknown conn_id → 返回 False 不抛异常。"""
    manager = NetworkManager()
    manager.configure(_make_config())
    handled = await manager.handle_inbound("not-registered", {"kind": "ping", "ts": 1})
    assert handled is False


@pytest.mark.asyncio
async def test_register_without_configure_raises() -> None:
    """未调 configure 就 register → RuntimeError("call configure() first")。"""
    manager = NetworkManager()
    ws = _make_ws()
    with pytest.raises(RuntimeError, match="configure"):
        await manager.register("claude", ws)


@pytest.mark.asyncio
async def test_send_success_keeps_connection() -> None:
    """send 写盘成功 → 返回 True + 连接仍在映射里。"""
    manager = NetworkManager()
    manager.configure(_make_config())
    ws = _make_ws()
    conn_id = await manager.register("claude", ws)

    ok = await manager.send(conn_id, {"kind": "custom", "payload": "x"})
    assert ok is True
    assert conn_id in manager._connections
    ws.send_json.assert_called_once_with({"kind": "custom", "payload": "x"})


@pytest.mark.asyncio
async def test_send_failure_triggers_unregister() -> None:
    """send 抛异常 → safe_send_json 返 False → 自动 unregister。"""
    manager = NetworkManager()
    manager.configure(_make_config())
    ws = _make_ws()

    async def _raise(_payload: Any) -> None:
        raise ConnectionError("simulated broken pipe")

    ws.send_json = _raise
    conn_id = await manager.register("claude", ws)

    ok = await manager.send(conn_id, {"kind": "x"})
    assert ok is False
    assert conn_id not in manager._connections
    assert conn_id not in manager._heartbeats


@pytest.mark.asyncio
async def test_send_unknown_conn_id_returns_false() -> None:
    """unknown conn_id 的 send → False 不抛。"""
    manager = NetworkManager()
    manager.configure(_make_config())
    ok = await manager.send("not-registered", {"kind": "x"})
    assert ok is False
