"""UsageTokenManager v2 —— 无状态门面，唯一公共入口。

⚠️ **本模块是 ``web.usage_token_v2`` 包内仅有的对外暴露入口**。外部模块禁止：

- 直接 import 内部派生器（``_derive_claude`` / ``_derive_codex`` / ``_derive_generic``）
- 直接 import 私有模型（``_models`` / ``_model_context_table``）
- 跨 manager 读 SDK jsonl/rollout 文件（统一走本 manager API）

设计哲学（v2，参见 docs/usage-token-v2/）：

- **无状态**：``__init__`` 只接 locator，运行时不持有任何长期数据
- **真源在 SDK**：token 数据来自 SDK 自己写的 jsonl/rollout
- **分通道 DTO**：返回 ``ClaudeUsage`` / ``CodexUsage`` / ``GenericChatAnthropicUsage``
  / ``GenericChatOpenAIUsage`` 之一，自带 ``provider`` discriminator
- **公共 API 只有 1 个**：``get_thread_usage(thread_id)``

并发安全：完全无共享状态 → 100 路并发查不同 thread 互不影响。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from web.usage_token_v2._derive_claude import derive_from_jsonl as _derive_claude
from web.usage_token_v2._derive_codex import derive_from_rollout as _derive_codex
from web.usage_token_v2._derive_generic import (
    derive_from_session as _derive_generic,
)
from web.usage_token_v2._models import ThreadUsage

logger = logging.getLogger(__name__)

ProviderKind = Literal["anthropic", "openai_compatible"]


# =============================================================================
# 注入 Protocol（4 个 locator，由装配层实现）
# =============================================================================


@runtime_checkable
class ThreadMetadataReader(Protocol):
    """读 thread 的轻量元数据（不含 token 字段，schema v9 之后）。

    返回 dict 至少包含：

    - ``backend_kind``: ``"claude_code"`` / ``"codex"`` / ``"generic_chat"``
    - 其他字段（cwd / claude_thread_id / codex_thread_id / preset_id）按需

    用 dict 而非 ThreadMetadata 实例避免 manager import thread_metadata。
    """

    async def read(self, thread_id: str) -> dict[str, Any] | None:
        """读 thread 元数据；不存在返回 None。"""
        ...


@runtime_checkable
class ClaudeJsonlLocator(Protocol):
    """thread_id → Claude SDK jsonl 路径。

    非 claude_code 通道 / 未首次绑定（缺 cwd 或 claude_thread_id）→ 返回 None。
    """

    async def locate(self, thread_id: str) -> Path | None: ...


@runtime_checkable
class CodexRolloutLocator(Protocol):
    """thread_id → Codex rollout 路径。

    非 codex 通道 / 扫描找不到匹配 codex_thread_id 的 rollout → 返回 None。
    """

    async def locate(self, thread_id: str) -> Path | None: ...


@runtime_checkable
class GenericChatSessionLocator(Protocol):
    """thread_id → (FileSession messages.jsonl 路径, provider 厂商)。

    返回 None 的几种情况：

    - 非 generic_chat 通道
    - session backend 不是 FileSession（memory / sqlite 不支持派生）
    - thread 未跑过 / session 文件未 materialize
    - 找不到 preset_id 对应的 provider 厂商
    """

    async def locate(self, thread_id: str) -> tuple[Path, ProviderKind] | None: ...


# =============================================================================
# UsageTokenManager v2
# =============================================================================


class UsageTokenManager:
    """token 用量唯一公共查询入口。**完全无状态**。

    构造时只接注入的 locator；运行时不维护任何缓存 / dict / lock / task。
    多 tab 同时打开多个 thread → N 路并发查询互不影响（天然线程安全）。
    """

    def __init__(
        self,
        *,
        meta_reader: ThreadMetadataReader,
        claude_locator: ClaudeJsonlLocator,
        codex_locator: CodexRolloutLocator,
        generic_locator: GenericChatSessionLocator,
    ) -> None:
        """注入 4 个查询者；不持有任何长期数据。

        Args:
            meta_reader: thread 元数据读取器（返回 backend_kind 等）
            claude_locator: claude_code 通道 jsonl 路径定位器
            codex_locator: codex 通道 rollout 路径定位器
            generic_locator: generic_chat 通道 session jsonl + provider 定位器
        """
        self._meta = meta_reader
        self._claude = claude_locator
        self._codex = codex_locator
        self._generic = generic_locator

    async def get_thread_usage(self, thread_id: str) -> ThreadUsage | None:
        """唯一公共方法：返回 thread 当前 token 用量 DTO。

        步骤：

        1. 读 thread metadata 拿 ``backend_kind``（只读 IO）
        2. 按 backend_kind 派发对应 locator + 派生器
        3. 派生器扫 SDK 真源 jsonl/rollout，返回 channel-specific DTO

        Args:
            thread_id: thread ID

        Returns:
            ``ClaudeUsage`` / ``CodexUsage`` / ``GenericChatAnthropicUsage``
            / ``GenericChatOpenAIUsage`` 之一，或 ``None``（thread 未绑定 SDK
            真源 / 真源不存在 / 派生失败）

        派生器任何异常都被静默吞掉返回 None，不影响主对话流。
        """
        try:
            meta = await self._meta.read(thread_id)
        except Exception:
            logger.warning(
                "get_thread_usage: meta_reader.read failed for %s",
                thread_id,
                exc_info=True,
            )
            return None
        if meta is None:
            return None

        backend = meta.get("backend_kind")
        try:
            if backend == "claude_code":
                return await self._dispatch_claude(thread_id)
            if backend == "codex":
                return await self._dispatch_codex(thread_id)
            if backend == "generic_chat":
                return await self._dispatch_generic(thread_id)
        except Exception:
            logger.warning(
                "get_thread_usage: dispatch failed for %s (backend=%s)",
                thread_id,
                backend,
                exc_info=True,
            )
            return None

        # 未知 backend_kind
        return None

    # ------------------------------------------------------------------
    # 私有派发（按 backend_kind 路由）
    # ------------------------------------------------------------------

    async def _dispatch_claude(self, thread_id: str) -> ThreadUsage | None:
        path = await self._claude.locate(thread_id)
        if path is None:
            return None
        return await asyncio.to_thread(_derive_claude, path)

    async def _dispatch_codex(self, thread_id: str) -> ThreadUsage | None:
        path = await self._codex.locate(thread_id)
        if path is None:
            return None
        return await asyncio.to_thread(_derive_codex, path)

    async def _dispatch_generic(self, thread_id: str) -> ThreadUsage | None:
        located = await self._generic.locate(thread_id)
        if located is None:
            return None
        path, provider = located
        return await asyncio.to_thread(_derive_generic, path, provider)


__all__ = [
    "ClaudeJsonlLocator",
    "CodexRolloutLocator",
    "GenericChatSessionLocator",
    "ProviderKind",
    "ThreadMetadataReader",
    "UsageTokenManager",
]
