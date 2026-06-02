"""Heartbeat v0.1 骨架（单连接心跳状态机）。

设计真源：``docs/web-network-layer-v0.1/02-module-breakdown.md``
``Heartbeat 设计`` 节。

核心约束：

- **一个 ``Heartbeat`` 实例 = 一条连接的心跳状态机**，不是全局单例；
  ``NetworkManager`` 持有 ``dict[conn_id, Heartbeat]``，N 连接 = N 个
  实例并存，互不串扰
- ``Heartbeat`` 类**纯被动**：不知道底层 socket、不知道 channel 类型，
  只通过 ``HeartbeatHooks`` 与外界交互
- 多实例并发安全是核心约束；任何共享可变状态都必须放在实例字段内，
  类作用域 / 模块作用域**不允许**出现可变状态

v0.1 后端简化：后端是 server 角色（被动响应 client 发的 ping），所以
本版本 ``Heartbeat`` 只暴露 ``handle_inbound_frame(frame) -> dict | None``
处理 inbound ping 并返回应回的 pong，不需要主动发 ping。接口仍预留
双向能力，v0.3 升级到 server-push ping 时不用动 client 端。

.. note::

   v0.1 step 1 阶段所有方法 body 是 ``raise NotImplementedError``，
   行为实现在 step 3（Heartbeat 状态机）补齐。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol


class HeartbeatHooks(Protocol):
    """``Heartbeat`` 与外界交互的回调接口。

    v0.1 后端 server 角色只需要两个 hook：``is_open`` 判断底层 socket
    是否可写、``send_pong`` 发回 pong 帧；``send_ping`` / ``on_latency``
    等主动探测语义留给 v0.3 server-push 升级时再加，本版本不引入避免
    Protocol 接口漂移。
    """

    def is_open(self) -> bool:
        """返回底层 socket 是否仍可写。"""
        ...

    async def send_pong(self, frame: dict[str, Any]) -> None:
        """把一条 pong 帧写回 socket。

        :param frame: 已构造好的 pong 帧 dict（包含 ``kind`` /
            ``timestamp_ms`` / ``ts`` 等字段，schema 见
            ``src/web/protocol/ws_frames.py::PongFrame``）
        """
        ...


@dataclass
class HeartbeatConfig:
    """心跳参数；**不设默认值**，必须由调用方传入。

    强制走 ``cfg.web.ws_heartbeat_*`` 三件套真源（已存在），不在
    ``network/`` 层立新常量；所有频道未来共用同一份配置（"所有通道
    公用一个倒计时配置"原则，spec 04 明确禁止覆盖）。

    :ivar interval_ms: 期望相邻两次 ping 的间隔毫秒数；server 角色
        v0.1 不主动发 ping，本字段当前仅作 metadata 备 v0.3 用
    :ivar timeout_ms: 单次 pong 等待超时毫秒数；同上 v0.1 仅 metadata
    :ivar max_missed: 连续未回的 pong 次数上限；同上 v0.1 仅 metadata
    """

    interval_ms: int
    timeout_ms: int
    max_missed: int


class Heartbeat:
    """单连接心跳状态机；``NetworkManager`` per-connection 实例化。

    实例字段全部私有（``_`` 前缀）；多实例并发安全靠"每实例独立持有
    可变状态"实现，不依赖全局锁。
    """

    def __init__(self, config: HeartbeatConfig, hooks: HeartbeatHooks) -> None:
        self._config: HeartbeatConfig = config
        self._hooks: HeartbeatHooks = hooks
        self._missed_pongs: int = 0
        self._pending_ping_ts: int | None = None
        self._latency_ms: int | None = None
        self._started: bool = False

    async def handle_inbound_frame(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条入站帧；若为 ping 则返回应回的 pong 帧 dict。

        非心跳相关帧（``kind != "ping"``）返回 ``None``，让上层把帧路由给
        业务 handler。

        服务端 v0.1 角色是 **被动响应**：客户端按 ``intervalMs`` 周期发 ping，
        本方法识别后原样 echo 客户端的 ``ts`` 用于 RTT 计算，并附带服务端
        当前 wall-clock ``timestamp_ms`` 作为信息字段（前端不参与 RTT，仅
        用于服务端时间漂移诊断）。pong 字段 schema 兼容
        :class:`web.protocol.ws_frames.PongFrame`（多出的 ``timestamp_ms``
        被 Pydantic ``extra`` 默认忽略）。

        纯函数式实现，多实例并发安全：不读写任何实例可变状态，所有状态
        来自入参 ``frame``，副作用为 ``None``（pong 发送由上层 ``send_pong``
        hook 负责）。

        :param frame: 已被上层解析为 ``dict`` 的入站帧
        :returns: 若 ``frame["kind"] == "ping"``，返回 pong 帧 dict
            ``{"kind": "pong", "ts": <原 ts>, "timestamp_ms": <服务端 ms>}``；
            否则 ``None``
        """
        if not isinstance(frame, dict):
            return None
        if frame.get("kind") != "ping":
            return None
        return {
            "kind": "pong",
            "ts": frame.get("ts"),
            "timestamp_ms": int(time.time() * 1000),
        }

    @property
    def latency_ms(self) -> int | None:
        """最近一次 RTT；v0.1 后端 server 角色不主动探测，恒为 ``None``。"""
        return self._latency_ms
