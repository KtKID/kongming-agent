"""network 层私有工具模块（v0.1）。

**边界（import-linter Contract 12 强制）**：本模块只允许 ``src/network/``
包内部 import；业务模块（``web`` 等）若发现需要 import 这里的某个函数，
说明该函数**不属于** network 层，应重新评估归属——要么是业务工具应留
在业务侧，要么是真该升级为 ``network/manager`` 或 ``network/heartbeat``
的公开接口；不要把私有工具暴露出去。

设计真源：``docs/web-network-layer-v0.1/02-module-breakdown.md`` ``tools
脚本`` 节。

v0.1 提供的工具：

- :func:`make_connection_id` —— 生成全局唯一的连接 ID（uuid4 hex）
- :func:`safe_send_json` —— 向 WebSocket 发送 JSON 帧，失败返回 ``False``，
  调用方据此触发清理（语义抄 ``src/web/ws_fanout.py::WebSocketFanout``）
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any


def make_connection_id() -> str:
    """生成一个进程级唯一的连接 ID。

    v0.1 直接用 ``uuid.uuid4().hex``（32 字符 lowercase hex，无连字符）。
    NetworkManager 的 ``conn_id`` 命名约定为 ``"<channel>:<scope>"``（详见
    spec 04），但本函数只负责生成 ID 字面值的"唯一后缀"部分；调用方拼好
    最终 ``conn_id`` 后传给 ``ConnectionInfo``。当前 v0.1 后端实现里
    ``NetworkManager.register`` 直接把本函数返回值当 ``conn_id``，命名
    约定的 ``channel`` / ``thread_id`` 信息走 ``ConnectionInfo`` 字段记录
    而不写进 ID 字符串——v0.2 接入多频道时可按需扩展。

    :returns: 32 字符 lowercase hex 字符串
    """
    return uuid.uuid4().hex


async def safe_send_json(ws: Any, payload: Any) -> bool:
    """向一个 WebSocket 发送 JSON 帧，捕获并吞掉所有底层异常。

    语义抄 :class:`web.ws_fanout.WebSocketFanout` 的失败连接清理模式：

    - 写盘成功 → 返回 ``True``
    - 写盘抛任何异常（连接已断 / 半开 / runtime error）→ 返回 ``False``，
      不抛出；调用方据此判断是否清理 ``conn_id`` 映射

    v0.1 不对 ``ws`` 做类型约束（fastapi ``WebSocket`` / starlette
    ``WebSocket`` / mock ``AsyncMock`` 都得能用），用 ``Any`` 接受所有
    具有 ``async def send_json`` 方法的对象。

    :param ws: 目标 WebSocket 实例（已 ``accept`` 状态）
    :param payload: 任意可被 ``json.dumps`` 序列化的对象
    :returns: 写盘是否成功
    """
    try:
        await ws.send_json(payload)
    except Exception:
        with contextlib.suppress(Exception):
            # 不主动 close ws：close 由 NetworkManager.unregister 统一收尾，
            # 这里 swallow 任何二次异常即可，避免堆栈污染 caller。
            pass
        return False
    return True


__all__ = [
    "make_connection_id",
    "safe_send_json",
]
