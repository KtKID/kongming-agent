"""UsageTokenManager —— Token 用量唯一对外入口。

⚠️ **本模块是 ``web.usage_token`` 包内仅有的对外暴露入口**。外部模块禁止：

- 直接 import ``_models / _channel_anthropic / _channel_openai / _persistence /
  _migration``（``.importlinter`` Contract 强制）
- 直接构造 ``TokenUsage`` 实例（在 _models 私有）
- 跨 manager 读写 ``ThreadMetadata.cumulative_usage`` dict（统一走本 manager API）

公共 API（7 方法）：

- ``set_last_assistant_usage`` —— 单次 API call 完成后更新 last_snapshot（双写：内存 + 落盘）
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
from typing import Any, Literal, Protocol, runtime_checkable

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
from web.usage_token._derive_claude_code import (
    ClaudeCodeDerived,
    derive_from_jsonl,
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


@runtime_checkable
class DeriveProvider(Protocol):
    """claude_code 通道 jsonl 路径解析器。

    让外部（``thread_manager`` 装配层）告诉 manager 怎么从 thread_id 找到对应
    的 SDK jsonl 文件。manager 内部消费私有 ``_derive_claude_code`` 派生函数；
    外部模块只实现"返回路径"这个最薄接口，避免接触 ``ClaudeCodeDerived`` 私有 DTO，
    也避免跨越 ``.importlinter`` Contract 9 边界 import ``_derive_claude_code``
    子模块。

    实现要点（参见 ``web.thread_manager._ClaudeCodeJsonlPathProvider``）：

    - 非 ``backend_kind="claude_code"`` 的 thread → 返回 ``None``
    - 缺 ``claude_thread_id`` / ``cwd`` 的 thread（未首次绑定）→ 返回 ``None``
    - 拼 ``~/.claude/projects/<encoded-cwd>/<claude_thread_id>.jsonl`` 路径
      （**不**校验文件存在，让 ``derive_from_jsonl`` 内部容错）
    """

    async def get_claude_jsonl_path(self, thread_id: str) -> Path | None:
        """返回该 thread 对应的 SDK jsonl 路径，或 ``None`` 表示不走派生。"""
        ...


class UsageTokenManager:
    """所有 token usage 访问的唯一入口。

    线程安全：内部 per-thread ``asyncio.Lock`` 防止并发 ``record_run_usage`` 竞争。

    缓存策略：

    - ``_last_snapshot``：最近一轮 ``UsageTokenSnapshot`` 内存缓存（emit WS 帧用）
    - ``_thread_models``：thread 当前 model 名内存缓存
    - 进程重启后首次访问 lazy load 从盘恢复（``_ensure_snapshot_loaded`` /
      ``_ensure_model_loaded``）作为 **fallback**；claude_code 通道优先走
      ``derive_provider`` 从 SDK jsonl 现场派生（参见
      ``usage-token-derive-from-jsonl-v0.1`` task 包）

    外部模块禁止直接持有 ``TokenUsage`` 实例。
    """

    def __init__(
        self,
        home: Path,
        *,
        thread_metadata_io: ThreadMetadataIO,
        model_context_windows: Mapping[str, int] | None = None,
        derive_provider: DeriveProvider | None = None,
    ) -> None:
        """构造 manager。

        Args:
            home: ``.kongming/`` 根目录（用于将来扩展，目前持久化全走 ``thread_metadata_io``）
            thread_metadata_io: ``ThreadMetadata.cumulative_usage`` 读写抽象
                （task#1 用 ``InMemoryThreadMetadataIO`` 测试用 dummy；
                task#2 真实 adapter）
            model_context_windows: 模型名 → context window 上限的内置表
                （留空 = 全部未命中；task#4 在前端管占位表）
            derive_provider: claude_code 通道派生器（**v0.1 新增**）。注入后
                ``get_thread_summary`` / ``get_last_run_snapshot`` 优先用 jsonl
                派生结果，避开 metadata.json 写盘并发竞争。``None`` 表示禁用派生，
                全部走 metadata 读盘（向后兼容；单测可注入 ``None``）。
        """
        self._home = home
        self._io = thread_metadata_io
        self._model_context_windows: dict[str, int] = dict(model_context_windows or {})
        self._derive_provider = derive_provider
        # per-thread Lock 防并发竞争
        self._locks: dict[str, asyncio.Lock] = {}
        # 最近一轮 snapshot 内存缓存（emit WS 帧用；不再写盘）
        self._last_snapshot: dict[str, UsageTokenSnapshot] = {}
        # thread 当前绑定模型名内存缓存（不再写盘）
        self._thread_models: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 模型上限注入（task#3/#4 wiring 用，不在公共 6 API 之列；
    # 视为 manager 的外部协作 API，task#1 阶段先提供）
    # ------------------------------------------------------------------

    def set_thread_model(self, thread_id: str, model_name: str) -> None:
        """注入 thread 当前绑定的模型名（用来查 context_window 上限）。

        **v0.1（usage-token-derive-from-jsonl）**：纯内存写入，**不再 fire-and-forget
        写盘**——claude_code 通道的 model_name 从 jsonl 现场派生（``derive_provider``）
        恢复，不需要 metadata.json 落盘。消除并发写盘竞争（参见 task 包
        ``usage-token-derive-from-jsonl-v0.1``）。

        Args:
            thread_id: thread ID
            model_name: 模型名（如 ``"claude-opus-4"``）；空字符串表示未知
        """
        if not model_name:
            self._thread_models.pop(thread_id, None)
        else:
            self._thread_models[thread_id] = model_name

    # ------------------------------------------------------------------
    # 公共 API 0（辅助）：set_last_assistant_usage
    # ------------------------------------------------------------------

    def set_last_assistant_usage(
        self,
        thread_id: str,
        *,
        channel: Channel,
        raw_payload: Mapping[str, Any],
        model: str | None = None,
    ) -> UsageTokenSnapshot | None:
        """更新 thread 的 last_snapshot——用于"上下文占用"显示。

        跟 ``record_run_usage`` 的区别：

        - ``record_run_usage`` 是 query 结束（``ResultMessage``）触发，
          **更新 cumulative + 落盘 + 更新 last_snapshot**（在 per-thread lock 内）
        - ``set_last_assistant_usage`` 是单次 API call 完成（``AssistantMessage``）
          触发，**纯内存更新 last_snapshot**（不动 cumulative，不写盘）

        为什么需要这个 API：``ResultMessage.usage`` 是 query 内多 API call 累加，
        跟 context window 比例无意义；``AssistantMessage.usage`` 是单次 API call
        的完整 prompt size（含 session 累积历史），跟 context window 比例才有意义。
        实时 emit WS 帧让前端立即看到 context 占用刷新。

        **v0.1（usage-token-derive-from-jsonl）**：去掉 fire-and-forget 写盘——
        claude_code 通道的 last_snapshot 重启后从 jsonl 派生恢复（``derive_provider``），
        不再依赖 metadata.json 落盘。消除并发写盘竞争。

        ``raw_payload`` 为空 dict 时返回 ``None``，不更新 ``_last_snapshot``。

        Args:
            thread_id: thread ID
            channel: ``"anthropic"`` / ``"openai"``
            raw_payload: SDK 单次 API call 的 usage dict
            model: 可选模型名（如 ``"claude-opus-4"``）——自动注入
                ``_thread_models`` 缓存

        Returns:
            新写入的 ``UsageTokenSnapshot``，或 ``None``（payload 无效时）

        Raises:
            ValueError: channel 不在已知值
        """
        if channel not in ("anthropic", "openai"):
            raise ValueError(f"unknown channel: {channel!r}")
        if not raw_payload:
            return None
        if model:
            self.set_thread_model(thread_id, model)
        # 复用 _parse + _to_snapshot，但不调 lock / 不写盘 / 不动 cumulative
        delta = self._parse(channel, raw_payload)
        snapshot = self._to_snapshot(channel, delta, turn=0, run_id="")
        self._last_snapshot[thread_id] = snapshot
        return snapshot

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
        model: str | None = None,
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
            model: 本轮使用的模型名（如 ``"claude-opus-4"``）；传入时自动
                调 ``set_thread_model`` 缓存到内存（省去调用方手动注入）

        Returns:
            ``UsageTokenSnapshot``（含本轮 input / output / extras / context_usage）

        Raises:
            ValueError: channel 不在已知值
            pydantic.ValidationError: raw_payload 解析失败
        """
        if channel not in ("anthropic", "openai"):
            raise ValueError(f"unknown channel: {channel!r}")

        # 自动注入 model 到 thread 内存缓存（fallback 路径用；写盘在 lock 内同步进行）
        if model:
            self.set_thread_model(thread_id, model)

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

            # 4. persist cumulative
            await self._io.write_cumulative_usage(thread_id, to_persisted_dict(new_cumulative))

            # 5. cache + construct snapshot（用 delta，不是 cumulative——
            # snapshot 是"本轮"用量）
            snapshot = self._to_snapshot(channel, delta, turn=turn, run_id=run_id)
            self._last_snapshot[thread_id] = snapshot
            # 6. persist snapshot + model_name（lock 内串行，无并发竞争）。
            #    claude_code 通道下 get_thread_summary 走派生 override，这里写的
            #    last_run_snapshot 是 fallback 路径用；codex / generic_chat 通道
            #    则强依赖这两个字段做 fallback。
            try:
                await self._io.write_last_run_snapshot(thread_id, snapshot.model_dump(mode="json"))
            except (KeyError, OSError) as exc:
                logger.debug("persist snapshot skipped for %s: %s", thread_id, exc)
            if model:
                try:
                    await self._io.write_last_model_name(thread_id, model)
                except (KeyError, OSError) as exc:
                    logger.debug("persist model_name skipped for %s: %s", thread_id, exc)
            return snapshot

    # ------------------------------------------------------------------
    # 公共 API 2/6：get_thread_summary
    # ------------------------------------------------------------------

    async def get_thread_summary(self, thread_id: str) -> ThreadUsageSummary:
        """返回 thread 级累计 summary（前端 hydrate / StatusLine 渲染用）。

        **v0.1（usage-token-derive-from-jsonl）**：claude_code 通道优先走
        ``derive_provider`` 派生（jsonl 真源），失败 fallback 到 metadata 读盘。
        派生成功时会顺手把内存 ``_last_snapshot`` / ``_thread_models`` 同步成
        派生结果，让 ``get_last_run_snapshot`` / WS 帧拿到一致数据。

        Args:
            thread_id: thread ID

        Returns:
            ``ThreadUsageSummary``。如果 thread 还没跑过任何 turn，返回 channel
            根据已注入的 model 推断；都不知道时回退为 ``"anthropic"`` 全零
            （前端不会因为字段缺失而崩溃）。
        """
        # 优先派生：claude_code 通道直接从 jsonl 真源现算
        derived_summary = await self._summary_from_derive(thread_id)
        if derived_summary is not None:
            return derived_summary

        # Fallback：lazy load 旧 metadata 读盘路径（codex / generic_chat / 派生失败兜底）
        await self._ensure_snapshot_loaded(thread_id)
        await self._ensure_model_loaded(thread_id)

        cumulative_dict = await self._io.read_cumulative_usage(thread_id)
        cumulative = from_persisted_dict(cumulative_dict)

        # 取最近一轮 snapshot 算 last_run_context_usage（无则 0）
        last_snap = self._last_snapshot.get(thread_id)

        # 取模型名 + 上限
        model_name = self._thread_models.get(thread_id, _UNKNOWN_MODEL)
        ctx_window = self.context_window(model_name) if model_name else None

        if cumulative is None:
            # 首轮前：返回全零 anthropic（前端容错；channel 一会儿在 record_run_usage
            # 真正落实后会重置）。
            # 但 set_last_assistant_usage 可能已写入 _last_snapshot（ResultMessage
            # 尚未到达），此时 last_run_context_usage 应反映该值。
            last_ctx = last_snap.context_usage if last_snap is not None else 0
            pct: float | None = None
            if ctx_window and ctx_window > 0 and last_ctx > 0:
                pct = round(100.0 * last_ctx / ctx_window, 2)
            return ThreadUsageSummary(
                channel="anthropic",
                cumulative_input_tokens=0,
                cumulative_output_tokens=0,
                extras={},
                last_run_context_usage=last_ctx,
                model_name=model_name,
                model_context_window=ctx_window,
                context_usage_pct=pct,
            )

        channel: Channel = cumulative.channel
        if channel == "anthropic":
            assert isinstance(cumulative, _AnthropicTokenUsage)
            extras = _anthropic_extras(cumulative)
        else:
            assert isinstance(cumulative, _OpenAITokenUsage)
            extras = _openai_extras(cumulative)

        last_ctx = last_snap.context_usage if last_snap is not None else 0
        pct = None
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

        **v0.1（usage-token-derive-from-jsonl）**：claude_code 通道优先走派生
        （jsonl 真源），失败 fallback 到内存 ``_last_snapshot`` + lazy load。
        """
        # 优先派生
        if self._derive_provider is not None:
            derived = await self._safe_derive(thread_id)
            if derived is not None:
                self._sync_caches_from_derive(thread_id, derived)
                return derived.last_snapshot

        # Fallback
        await self._ensure_snapshot_loaded(thread_id)
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
        await self._ensure_snapshot_loaded(thread_id)
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
    # 内部辅助方法：派生 + lazy load
    # ==================================================================

    async def _safe_derive(self, thread_id: str) -> ClaudeCodeDerived | None:
        """通过 derive_provider 拿 jsonl 路径并调派生函数；异常一律视为派生失败。

        派生不应该影响主链路；失败时静默 fallback 到 metadata 读盘。
        I/O 都跑在 ``asyncio.to_thread`` 里避免阻塞事件循环。
        """
        if self._derive_provider is None:
            return None
        try:
            path = await self._derive_provider.get_claude_jsonl_path(thread_id)
            if path is None:
                return None
            return await asyncio.to_thread(derive_from_jsonl, path)
        except Exception:
            logger.warning(
                "derive_provider failed for %s; falling back to metadata read",
                thread_id,
                exc_info=True,
            )
            return None

    def _sync_caches_from_derive(self, thread_id: str, derived: ClaudeCodeDerived) -> None:
        """把派生结果同步到内存 caches，让 WS 帧 / 后续 set_last_assistant_usage 一致。"""
        self._last_snapshot[thread_id] = derived.last_snapshot
        if derived.last_model_name:
            self._thread_models[thread_id] = derived.last_model_name

    async def _summary_from_derive(self, thread_id: str) -> ThreadUsageSummary | None:
        """尝试用 derive_provider 构造 summary；失败返回 None。"""
        derived = await self._safe_derive(thread_id)
        if derived is None:
            return None
        self._sync_caches_from_derive(thread_id, derived)
        model_name = derived.last_model_name or self._thread_models.get(thread_id, _UNKNOWN_MODEL)
        ctx_window = self.context_window(model_name) if model_name else None
        last_ctx = derived.last_snapshot.context_usage
        pct: float | None = None
        if ctx_window and ctx_window > 0:
            pct = round(100.0 * last_ctx / ctx_window, 2)
        return ThreadUsageSummary(
            channel="anthropic",
            cumulative_input_tokens=derived.cumulative.input_tokens,
            cumulative_output_tokens=derived.cumulative.output_tokens,
            extras=_anthropic_extras(derived.cumulative),
            last_run_context_usage=last_ctx,
            model_name=model_name,
            model_context_window=ctx_window,
            context_usage_pct=pct,
        )

    async def _ensure_snapshot_loaded(self, thread_id: str) -> None:
        """Lazy load：内存无 snapshot 时从盘上恢复（仅一次；后续走内存）。"""
        if thread_id in self._last_snapshot:
            return
        raw = await self._io.read_last_run_snapshot(thread_id)
        if raw is not None:
            try:
                self._last_snapshot[thread_id] = UsageTokenSnapshot.model_validate(raw)
            except Exception:
                logger.debug("failed to restore snapshot from disk for %s", thread_id)

    async def _ensure_model_loaded(self, thread_id: str) -> None:
        """Lazy load：内存无 model 时从盘上恢复（仅一次；后续走内存）。"""
        if thread_id in self._thread_models:
            return
        name = await self._io.read_last_model_name(thread_id)
        if name:
            self._thread_models[thread_id] = name

    # ==================================================================
    # 内部辅助方法：通用
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


__all__ = ["Channel", "DeriveProvider", "UsageTokenManager"]
