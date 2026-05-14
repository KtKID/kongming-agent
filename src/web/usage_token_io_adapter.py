"""ThreadMetadataIOAdapter：满足 ``web.usage_token.ThreadMetadataIO`` Protocol
的真实实现，包装 ``web.thread_metadata.read_thread_metadata`` /
``write_thread_metadata`` 读写 ThreadMetadata 的 ``cumulative_usage`` 字段。

⚠️ **架构边界**：本 adapter 是 ``UsageTokenManager`` 跟 ``ThreadMetadata`` 落盘
之间的桥梁——manager 通过抽象 ``ThreadMetadataIO`` Protocol 读写；本 adapter
在 ThreadManager 装配阶段注入 manager（避免 manager 直接依赖 thread_metadata
模块）。

依赖方向：
    UsageTokenManager (in web.usage_token)
        ↓ (Protocol)
    ThreadMetadataIOAdapter (本模块)
        ↓ (concrete impl)
    web.thread_metadata.read_thread_metadata / write_thread_metadata
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from web.thread_metadata import read_thread_metadata, write_thread_metadata
from web.usage_token import ThreadMetadataIO

logger = logging.getLogger(__name__)


class ThreadMetadataIOAdapter:
    """ThreadMetadataIO Protocol 的真实实现（thread_metadata.json 落盘）。

    线程安全：``write_cumulative_usage`` 用 ``os.replace`` 原子写盘
    （``web.thread_metadata.write_thread_metadata`` 已保证）。
    并发由 ``UsageTokenManager`` 内部 per-thread ``asyncio.Lock`` 保护。
    """

    def __init__(self, home: Path) -> None:
        """构造 adapter。

        Args:
            home: ``.kongming/`` 根目录（跟 ThreadManager._home 一致）
        """
        self._home = home

    async def read_cumulative_usage(self, thread_id: str) -> dict[str, Any] | None:
        """读 ``ThreadMetadata.cumulative_usage`` dict 或 ``None``。

        ⚠️ thread 不存在 / metadata 损坏 → 返回 ``None``（不抛；manager 视为首轮前）。
        """
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            return None
        # ThreadMetadata.cumulative_usage 是 dict[str, Any] | None（透明落盘）
        return meta.cumulative_usage

    async def write_cumulative_usage(self, thread_id: str, usage_dict: dict[str, Any]) -> None:
        """把 ``cumulative_usage`` dict 写回 metadata.json。

        实现：读出当前 ThreadMetadata，``model_copy(update={cumulative_usage=...})``，
        原子写盘。

        Raises:
            KeyError: thread 不存在
        """
        meta = await asyncio.to_thread(read_thread_metadata, self._home, thread_id)
        if meta is None:
            raise KeyError(f"thread not found for usage write: {thread_id}")
        updated = meta.model_copy(update={"cumulative_usage": usage_dict})
        await asyncio.to_thread(write_thread_metadata, self._home, updated)


# Protocol 兼容性自检（启动时一次性，运行时零开销）
assert isinstance(ThreadMetadataIOAdapter(Path("/tmp")), ThreadMetadataIO)


__all__ = ["ThreadMetadataIOAdapter"]
