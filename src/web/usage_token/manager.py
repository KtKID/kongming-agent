"""UsageTokenManager —— Token 用量唯一对外入口。

⚠️ **本模块是 ``web.usage_token`` 包内仅有的对外暴露入口**。外部模块禁止：

- 直接 import ``_models / _channel_anthropic / _channel_openai / _persistence /
  _migration``（``.importlinter`` Contract 强制）
- 直接构造 ``TokenUsage`` 实例（在 _models 私有）
- 跨 manager 读写 ``ThreadMetadata.cumulative_usage`` dict（统一走本 manager API）

公共 API（仅 6 方法）：

- ``record_run_usage`` —— 每轮结束写盘
- ``get_thread_summary`` —— thread 级 summary 查询
- ``get_last_run_snapshot`` —— 最近一轮 snapshot 查询
- ``to_ws_frame`` —— 构造 WS 帧
- ``context_window`` —— 查模型上限
- ``context_usage_pct`` —— 查 thread 当前 context 占用百分比

设计依据：[`docs/usage-token/02-module-breakdown.md`](../../../docs/usage-token/02-module-breakdown.md#1-usagetokenmanager)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from web.usage_token._channel_anthropic import (
    context_usage as _anthropic_context_usage,
)
from web.usage_token._channel_anthropic import (
    merge_cumulative as _anthropic_merge,
)
from web.usage_token._channel_anthropic import (
    parse_raw_to_usage as _anthropic_parse,
)
from web.usage_token._channel_anthropic import (
    to_extras_dict as _anthropic_extras,
)
from web.usage_token._channel_openai import (
    context_usage as _openai_context_usage,
)
from web.usage_token._channel_openai import (
    merge_cumulative as _openai_merge,
)
from web.usage_token._channel_openai import (
    parse_raw_to_usage as _openai_parse,
)
from web.usage_token._channel_openai import (
    to_extras_dict as _openai_extras,
)
from web.usage_token._models import (
    ThreadUsageSummary,
    TokenUsage,
    UsageFrame,
    UsageTokenSnapshot,
    _AnthropicTokenUsage,
    _OpenAITokenUsage,
)
from web.usage_token._persistence import (
    ThreadMetadataIO,
    empty_usage_for_channel,
    from_persisted_dict,
    to_persisted_dict,
)

logger = logging.getLogger(__name__)


# Channel 类型别名（manager 公共 API 用），跟 _models.channel 字面值对齐
Channel = Literal["anthropic", "openai"]


# Thread 当前绑定的模型名解析 sentinel（manager 不感知 ThreadMetadata 细节，
# 由外部 task#3 wiring 时通过 set_thread_model 接口注入；task#1 阶段先用空字符串）
_UNKNOWN_MODEL = ""


class UsageTokenManager:
    """所有 token usage 访问的唯一入口。

    线程安全：内部 per-thread ``asyncio.Lock`` 防止并发 ``record_run_usage`` 竞争。

    缓存策略：

    - ``_last_snapshot``：最近一轮 ``UsageTokenSnapshot`` 内存缓存（``Dict[thread_id, Snapshot]``）
    - ``_thread_models``：thread 当前 model 名内存缓存（task#3 通过 ``set_thread_model`` 注入）
    - 进程重启缓存失效，但 ``cumulative_usage`` 从落盘恢复

    外部模块禁止直接持有 ``TokenUsage`` 实例。
    """

    def __init__(
        self,
        home: Path,
        *,
        thread_metadata_io: ThreadMetadataIO,
        model_context_windows: Mapping[str, int] | None = None,
    ) -> None:
        """构造 manager。

        Args:
            home: ``.kongming/`` 根目录（用于将来扩展，目前持久化全走 ``thread_metadata_io``）
            thread_metadata_io: ``ThreadMetadata.cumulative_usage`` 读写抽象
                （task#1 用 ``InMemoryThreadMetadataIO`` 测试用 dummy；
                task#2 真实 adapter）
            model_context_windows: 模型名 → context window 上限的内置表
                （留空 = 全部未命中；task#4 在前端管占位表）
        """
        self._home = home
        self._io = thread_metadata_io
        self._model_context_windows: dict[str, int] = dict(model_context_windows or {})
        # per-thread Lock 防并发竞争
        self._locks: dict[str, asyncio.Lock] = {}
        # 最近一轮 snapshot 内存缓存
        self._last_snapshot: dict[str, UsageTokenSnapshot] = {}
        # thread 当前绑定模型名缓存（task#3 wiring 时由外部注入）
        self._thread_models: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 模型上限注入（task#3/#4 wiring 用，不在公共 6 API 之列；
    # 视为 manager 的外部协作 API，task#1 阶段先提供）
    # ------------------------------------------------------------------

    def set_thread_model(self, thread_id: str, model_name: str) -> None:
        """注入 thread 当前绑定的模型名（用来查 context_window 上限）。

        task#3 wiring 时由 ``thread_manager`` 在创建 / resume thread 时调用。

        Args:
            thread_id: thread ID
            model_name: 模型名（如 ``"claude-opus-4"``）；空字符串表示未知
        """
        if not model_name:
            self._thread_models.pop(thread_id, None)
        else:
            self._thread_models[thread_id] = model_name

    # ------------------------------------------------------------------
    # 公共 API 1/6：record_run_usage
    # ------------------------------------------------------------------

    async def record_run_usage(
        self,
        thread_id: str,
        *,
        channel: Channel,
        raw_payload: Mapping[str, Any],
        turn: int,
        run_id: str,
    ) -> UsageTokenSnapshot:
        """每轮 LLM 调用结束后写盘 + 推 WS 用。

        内部流程：

        1. ``_channel_xxx.parse_raw_to_usage`` 解析 raw_payload → TokenUsage delta
        2. 读盘当前 ``cumulative_usage`` → from_persisted_dict → 上轮累计（或 empty）
        3. ``_channel_xxx.merge_cumulative`` 累加 → 新累计
        4. ``to_persisted_dict`` → 写回盘
        5. 缓存最近一轮 snapshot（key=thread_id）
        6. 返回构造好的 ``UsageTokenSnapshot`` 给 ws_event_sink

        Args:
            thread_id: thread ID
            channel: token 语义维度 ``"anthropic"`` / ``"openai"``
            raw_payload: SDK 原生 usage dict（按 channel 选 parser）
            turn: 本轮 turn 编号
            run_id: 本轮 run id（空字符串=未携带）

        Returns:
            ``UsageTokenSnapshot``（含本轮 input / output / extras / context_usage）

        Raises:
            ValueError: channel 不在已知值
            pydantic.ValidationError: raw_payload 解析失败
        """
        if channel not in ("anthropic", "openai"):
            raise ValueError(f"unknown channel: {channel!r}")

        async with self._get_lock(thread_id):
            # 1. parse delta
            delta = self._parse(channel, raw_payload)

            # 2. read prev cumulative
            prev_dict = await self._io.read_cumulative_usage(thread_id)
            prev_cumulative = from_persisted_dict(prev_dict)
            if prev_cumulative is None:
                prev_cumulative = empty_usage_for_channel(channel)
            else:
                # 防御性：如果存量的 channel 跟本轮不一致（理论上不应该），
                # 视为 schema 异常，按本轮 channel 重置。
                if prev_cumulative.channel != channel:
                    logger.warning(
                        "thread %s channel mismatch: stored=%s incoming=%s; resetting",
                        thread_id,
                        prev_cumulative.channel,
                        channel,
                    )
                    prev_cumulative = empty_usage_for_channel(channel)

            # 3. merge
            new_cumulative = self._merge(channel, prev_cumulative, delta)

            # 4. persist
            await self._io.write_cumulative_usage(thread_id, to_persisted_dict(new_cumulative))

            # 5. cache + construct snapshot（用 delta，不是 cumulative——
            # snapshot 是"本轮"用量）
            snapshot = self._to_snapshot(channel, delta, turn=turn, run_id=run_id)
            self._last_snapshot[thread_id] = snapshot
            return snapshot

    # ------------------------------------------------------------------
    # 公共 API 2/6：get_thread_summary
    # ------------------------------------------------------------------

    async def get_thread_summary(self, thread_id: str) -> ThreadUsageSummary:
        """返回 thread 级累计 summary（前端 hydrate / StatusLine 渲染用）。

        Args:
            thread_id: thread ID

        Returns:
            ``ThreadUsageSummary``。如果 thread 还没跑过任何 turn，返回 channel
            根据已注入的 model 推断；都不知道时回退为 ``"anthropic"`` 全零
            （前端不会因为字段缺失而崩溃）。
        """
        cumulative_dict = await self._io.read_cumulative_usage(thread_id)
        cumulative = from_persisted_dict(cumulative_dict)

        # 取最近一轮 snapshot 算 last_run_context_usage（无则 0）
        last_snap = self._last_snapshot.get(thread_id)

        # 取模型名 + 上限
        model_name = self._thread_models.get(thread_id, _UNKNOWN_MODEL)
        ctx_window = self.context_window(model_name) if model_name else None

        if cumulative is None:
            # 首轮前：返回全零 anthropic（前端容错；channel 一会儿在 record_run_usage
            # 真正落实后会重置）
            return ThreadUsageSummary(
                channel="anthropic",
                cumulative_input_tokens=0,
                cumulative_output_tokens=0,
                extras={},
                last_run_context_usage=0,
                model_name=model_name,
                model_context_window=ctx_window,
                context_usage_pct=None,
            )

        channel: Channel = cumulative.channel
        if channel == "anthropic":
            assert isinstance(cumulative, _AnthropicTokenUsage)
            extras = _anthropic_extras(cumulative)
        else:
            assert isinstance(cumulative, _OpenAITokenUsage)
            extras = _openai_extras(cumulative)

        last_ctx = last_snap.context_usage if last_snap is not None else 0
        pct: float | None = None
        if ctx_window and ctx_window > 0:
            pct = round(100.0 * last_ctx / ctx_window, 2)

        return ThreadUsageSummary(
            channel=channel,
            cumulative_input_tokens=cumulative.input_tokens,
            cumulative_output_tokens=cumulative.output_tokens,
            extras=extras,
            last_run_context_usage=last_ctx,
            model_name=model_name,
            model_context_window=ctx_window,
            context_usage_pct=pct,
        )

    # ------------------------------------------------------------------
    # 公共 API 3/6：get_last_run_snapshot
    # ------------------------------------------------------------------

    async def get_last_run_snapshot(self, thread_id: str) -> UsageTokenSnapshot | None:
        """返回 thread 最近一轮 ``UsageTokenSnapshot``，无则 ``None``。

        进程重启后缓存丢失（接受——StatusLine 会通过 get_thread_summary 兜底）。
        """
        return self._last_snapshot.get(thread_id)

    # ------------------------------------------------------------------
    # 公共 API 4/6：to_ws_frame
    # ------------------------------------------------------------------

    async def to_ws_frame(self, thread_id: str, *, turn: int, run_id: str) -> UsageFrame:
        """构造 WS S2C ``UsageFrame``（task#3 ws_event_sink emit 用）。

        Args:
            thread_id: thread ID
            turn: 本轮 turn（外部传入，跟 snapshot 对齐校验）
            run_id: 本轮 run id

        Returns:
            ``UsageFrame``，嵌套最近一轮 ``UsageTokenSnapshot``

        Raises:
            KeyError: thread 还没 record 过任何 usage（``to_ws_frame`` 应该在
                ``record_run_usage`` 之后调，否则调用顺序错）
        """
        snap = self._last_snapshot.get(thread_id)
        if snap is None:
            raise KeyError(f"no snapshot for thread {thread_id!r}; call record_run_usage first")
        return UsageFrame(
            kind="usage",
            turn=turn,
            run_id=run_id,
            snapshot=snap,
        )

    # ------------------------------------------------------------------
    # 公共 API 5/6：context_window
    # ------------------------------------------------------------------

    def context_window(self, model_name: str) -> int | None:
        """查模型 context window 上限。

        匹配规则：exact 优先；不命中尝试 prefix（按 key 长度倒序避免短前缀误命中）。

        Args:
            model_name: 模型名（如 ``"claude-opus-4"``）

        Returns:
            上限 token 数；未命中 ``None``
        """
        if not model_name:
            return None
        # exact match
        if model_name in self._model_context_windows:
            return self._model_context_windows[model_name]
        # prefix match（按 key 长度倒序，防止短前缀误命中）
        for key in sorted(self._model_context_windows.keys(), key=len, reverse=True):
            if model_name.startswith(key):
                return self._model_context_windows[key]
        return None

    # ------------------------------------------------------------------
    # 公共 API 6/6：context_usage_pct
    # ------------------------------------------------------------------

    def context_usage_pct(self, thread_id: str) -> float | None:
        """同步查 thread 当前 context 占用百分比。

        基于内存缓存（``_last_snapshot`` + ``_thread_models``）；不读盘。

        Args:
            thread_id: thread ID

        Returns:
            百分比（0-100，``round(2)``）；未命中模型表 / 无 snapshot → ``None``
        """
        snap = self._last_snapshot.get(thread_id)
        if snap is None:
            return None
        model_name = self._thread_models.get(thread_id)
        if not model_name:
            return None
        ctx_window = self.context_window(model_name)
        if not ctx_window or ctx_window <= 0:
            return None
        return round(100.0 * snap.context_usage / ctx_window, 2)

    # ==================================================================
    # 内部辅助方法
    # ==================================================================

    def _get_lock(self, thread_id: str) -> asyncio.Lock:
        """惰性创建并返回 per-thread Lock。"""
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[thread_id] = lock
        return lock

    def _parse(self, channel: Channel, raw: Mapping[str, Any]) -> TokenUsage:
        """按 channel 分发到对应 parser。"""
        if channel == "anthropic":
            return _anthropic_parse(raw)
        return _openai_parse(raw)

    def _merge(self, channel: Channel, prev: TokenUsage, delta: TokenUsage) -> TokenUsage:
        """按 channel 分发到对应 merger（确保 prev / delta 类型一致）。"""
        if channel == "anthropic":
            assert isinstance(prev, _AnthropicTokenUsage)
            assert isinstance(delta, _AnthropicTokenUsage)
            return _anthropic_merge(prev, delta)
        assert isinstance(prev, _OpenAITokenUsage)
        assert isinstance(delta, _OpenAITokenUsage)
        return _openai_merge(prev, delta)

    def _to_snapshot(
        self,
        channel: Channel,
        usage: TokenUsage,
        *,
        turn: int,
        run_id: str,
    ) -> UsageTokenSnapshot:
        """从 TokenUsage 派生公共 ``UsageTokenSnapshot``（外部 DTO）。"""
        if channel == "anthropic":
            assert isinstance(usage, _AnthropicTokenUsage)
            return UsageTokenSnapshot(
                channel="anthropic",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                extras=_anthropic_extras(usage),
                context_usage=_anthropic_context_usage(usage),
                turn=turn,
                run_id=run_id,
            )
        assert isinstance(usage, _OpenAITokenUsage)
        return UsageTokenSnapshot(
            channel="openai",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            extras=_openai_extras(usage),
            context_usage=_openai_context_usage(usage),
            turn=turn,
            run_id=run_id,
        )


__all__ = ["Channel", "UsageTokenManager"]
