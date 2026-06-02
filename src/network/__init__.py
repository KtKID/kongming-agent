"""网络层 v0.1 模块入口（骨架阶段）。

v0.1 范围：建立 ``NetworkManager`` 单例 + 独立 ``Heartbeat`` 状态机
脚本，仅接入 Claude 频道 WebSocket（``/ws/claude-code``）以治掉空闲约
60 秒沉默断开的问题；其他频道（generic_chat / codex / thread-status /
scheduler / workspace-shell）以及现有 3 个 broadcaster 的收编属于
v0.2 范围，**本版本不动**。

设计文档真源：

- ``docs/web-network-layer-v0.1/01-goals-and-boundaries.md`` —— 边界
- ``docs/web-network-layer-v0.1/02-module-breakdown.md`` —— 模块拆分
- ``docs/web-network-layer-v0.1/04-data-and-state.md`` —— 数据 / 状态

Import 边界（import-linter Contract 11 + 12 强制）：

- ``network`` 包不允许 import ``web``（业务层）
- ``network.tools`` 仅 ``network`` 包内部可见，业务层禁用

.. note::

   v0.1 step 1 当前阶段只建空骨架，所有方法 body 为
   ``raise NotImplementedError``；真正接入 Claude 频道 + 单例 import
   暴露在 step 4（NetworkManager 行为实现）+ step 6（claude WS endpoint
   接入）才补齐。下面 ``__all__`` 仅声明对外契约，**暂不真的 import
   manager 符号**，避免 step 1 之后到 step 4 之间任何文件意外消费空壳。
"""

from __future__ import annotations

from .heartbeat import Heartbeat, HeartbeatConfig, HeartbeatHooks
from .manager import ConnectionInfo, NetworkManager, get_network_manager

__all__: list[str] = [
    "ConnectionInfo",
    "Heartbeat",
    "HeartbeatConfig",
    "HeartbeatHooks",
    "NetworkManager",
    "get_network_manager",
]
