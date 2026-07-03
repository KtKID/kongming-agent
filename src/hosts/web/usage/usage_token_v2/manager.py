"""UsageTokenManager v2 —— 无状态门面，唯一公共入口。

⚠️ **本模块是 ``web.usage.usage_token_v2`` 包内仅有的对外暴露入口**。外部模块禁止：

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

from hosts.web.usage.usage_token_v2._derive_claude import derive_from_jsonl as _derive_claude
from hosts.web.usage.usage_token_v2._derive_codex import derive_from_rollout as _derive_codex
from hosts.web.usage.usage_token_v2._derive_generic import (
    derive_from_session as _derive_generic,
)
from hosts.web.usage.usage_token_v2._models import ThreadUsage

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
    """thread_id → (FileSession session JSONL 路径, provider 厂商)。

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
        # 注：per-agent 用量分桶（task-5）不在本 manager 上挂状态——本类是「完全无状态」
        # 查询入口（pre-push 测试断言此约束）。per-agent 累加器 AgentUsageBucket 是独立类，
        # 由装配层（EventSink 或 AgentManager）持有，不挂在 UsageTokenManager 上。
        # Event 已带 agent_id（task-1），SDK jsonl 真源无 agent_id，故 agent 维度从 Event 侧聚合。

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
    # agent-tree-v0.1 task-5：per-agent 用量分桶不在本 manager 上（本类无状态）。
    # 独立类 AgentUsageBucket（见文件末尾）由装配层持有；Event usage sink 调它累加。
    # 本 manager 的 get_thread_usage 仍走 SDK jsonl 真源（thread 级），不涉及 agent 维度。
    # ------------------------------------------------------------------

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
    "AgentUsageBucket",
    "ClaudeJsonlLocator",
    "CodexRolloutLocator",
    "GenericChatSessionLocator",
    "ProviderKind",
    "ThreadMetadataReader",
    "UsageTokenManager",
]


# ---------------------------------------------------------------------------
# agent-tree-v0.1 task-5：per-agent 用量累加（独立有状态类）
# ---------------------------------------------------------------------------
# 抽到独立类，让 UsageTokenManager 保持「完全无状态」（pre-push 测试断言此约束）。
# Event 已带 agent_id（task-1），SDK jsonl 真源无 agent_id，故 agent 维度只能从 Event 侧
# 聚合。进程内瞬态，重启归零（v1 接受，见 04-data-and-state.md 持久化方案）。
class AgentUsageBucket:
    """按 agent_id 累加 token 用量的进程内瞬态累加器。

    由 UsageTokenManager 持有一个实例；Event usage sink 调 ``record`` 累加，
    查询走 ``get`` / ``list_all``。无持久化，重启归零。
    """

    def __init__(self) -> None:
        self._buckets: dict[str, dict[str, int]] = {}

    def record(
        self,
        agent_id: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> dict[str, int]:
        """累加一次 agent 用量，返回该 agent 当前累计快照。"""
        bucket = self._buckets.setdefault(
            agent_id,
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
        bucket["prompt_tokens"] += prompt_tokens
        bucket["completion_tokens"] += completion_tokens
        bucket["total_tokens"] += total_tokens
        return dict(bucket)

    def get(self, agent_id: str) -> dict[str, int]:
        """查单 agent 累计；未记录返回全 0。"""
        bucket = self._buckets.get(agent_id)
        if bucket is None:
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return dict(bucket)

    def list_all(self) -> dict[str, dict[str, int]]:
        """列出所有 agent 累计快照（副本，调用方可安全迭代）。"""
        return {aid: dict(bucket) for aid, bucket in self._buckets.items()}
