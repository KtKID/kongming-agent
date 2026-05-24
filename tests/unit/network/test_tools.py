"""``network.tools`` 单元测试。

覆盖（dev-checklist 8.7）：

1. make_connection_id 返回唯一 uuid4 hex
2. safe_send_json 写盘成功 → True
3. safe_send_json 写盘抛异常 → False（不向外抛）
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from network.tools import make_connection_id, safe_send_json


def test_make_connection_id_returns_unique_uuid() -> None:
    """连续多次调用返回不同的 32 字符 lowercase hex 字符串。"""
    ids = {make_connection_id() for _ in range(64)}
    # 64 次调用全部唯一（uuid4 冲突概率忽略不计）
    assert len(ids) == 64
    for conn_id in ids:
        assert isinstance(conn_id, str)
        assert len(conn_id) == 32
        # hex lowercase
        assert all(c in "0123456789abcdef" for c in conn_id)


@pytest.mark.asyncio
async def test_safe_send_json_success_returns_true() -> None:
    """正常写盘 → 返回 True；ws.send_json 被传入原 payload。"""
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    payload = {"kind": "pong", "ts": 1234}
    ok = await safe_send_json(ws, payload)
    assert ok is True
    ws.send_json.assert_called_once_with(payload)


@pytest.mark.asyncio
async def test_safe_send_json_catches_error_returns_false() -> None:
    """ws.send_json 抛异常 → 返回 False；不向外冒泡。"""
    ws = AsyncMock()

    async def _raise(_payload: Any) -> None:
        raise ConnectionError("broken pipe")

    ws.send_json = _raise
    ok = await safe_send_json(ws, {"kind": "x"})
    assert ok is False


@pytest.mark.asyncio
async def test_safe_send_json_catches_runtime_error_returns_false() -> None:
    """starlette WebSocket 半开时常见的 RuntimeError 也被吞掉。"""
    ws = AsyncMock()

    async def _raise(_payload: Any) -> None:
        raise RuntimeError("Cannot call 'send' once a close message has been sent.")

    ws.send_json = _raise
    ok = await safe_send_json(ws, {"kind": "x"})
    assert ok is False
