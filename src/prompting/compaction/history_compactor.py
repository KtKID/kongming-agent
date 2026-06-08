"""最小历史压缩策略。

v1-mini 的压缩目标是 **不把 context 撑爆**，不是做高质量摘要。策略足够朴素：

1. 当历史长度超过阈值 ``max_messages`` 时才触发压缩；否则原样返回
2. 保留第一个 ``system`` 消息（如果存在且 ``keep_system=True``）——这通常是
   :mod:`prompting.instructions.instruction_loader` 渲染出的规则，丢掉它会让模型失忆
3. 保留最近 ``keep_recent`` 条——这是 agent loop 正在推进的工作区，不能截
4. 中间段：丢弃"无意义"的消息（没有 ``content`` 也没有 ``tool_calls`` 的
   assistant 消息），保留有内容的 user / assistant 消息；``tool`` 结果消息的
   ``content`` 如果超过 ``tool_result_max_chars``，则截断并追加标记
5. **不**做 LLM 摘要：摘要会引入 provider 调用，放到 v0.2+ 做（参考
   ``plan.md`` "Session 升级路径"）

这里**刻意**写成同步纯函数形式，然后通过 ``async def compact`` 暴露给上层——
未来替换成调 LLM 做摘要时，签名不用改。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.message import Message


@dataclass(frozen=True)
class CompactorConfig:
    """压缩策略参数。

    Attributes:
        max_messages: 历史长度超过此值才会进入压缩分支。
        keep_recent: 压缩后保留的最近消息条数（不含 system）。
        keep_system: 是否始终保留首个 ``system`` 消息。
        tool_result_max_chars: 单条 ``tool`` 结果消息 content 的最大字符数，超过则截断。
    """

    max_messages: int = 50
    keep_recent: int = 20
    keep_system: bool = True
    tool_result_max_chars: int = 2000


_TRUNCATED_SUFFIX = "\n...[truncated by history_compactor]"


class HistoryCompactor:
    """最小历史压缩器。

    用法::

        compactor = HistoryCompactor()
        new_history = await compactor.compact(old_history)

    ``compact`` 永远返回新 list（不就地修改入参）。``should_compact`` 公开给上层提前
    判断是否需要触发（例如记录压缩事件、在 trace 里打点）。
    """

    def __init__(self, config: CompactorConfig | None = None) -> None:
        self._cfg = config or CompactorConfig()

    @property
    def config(self) -> CompactorConfig:
        """暴露当前配置，便于上层日志 / trace 记录阈值。"""
        return self._cfg

    def should_compact(self, history: Sequence[Message]) -> bool:
        """当前历史是否需要压缩。"""
        return len(history) > self._cfg.max_messages

    async def compact(self, history: Sequence[Message]) -> list[Message]:
        """按策略压缩历史；不需要压缩时返回原样拷贝。

        为未来替换为 LLM 摘要预留 async 签名——当前实现是纯 CPU 工作。
        """
        if not self.should_compact(history):
            return list(history)
        return self._compact_sync(history)

    # -- 内部策略 ------------------------------------------------------------

    def _compact_sync(self, history: Sequence[Message]) -> list[Message]:
        """策略真正落地的地方。

        分三段组装：``head`` 放首条 system（可选），``tail`` 放最近 keep_recent 条，
        ``middle`` 筛过的中间段。
        """
        cfg = self._cfg
        history_list = list(history)
        if not history_list:
            return []

        # 1. 抽 head（首条 system）
        head: list[Message] = []
        head_index: int | None = None
        if cfg.keep_system:
            for idx, msg in enumerate(history_list):
                if msg.role == "system":
                    head.append(msg)
                    head_index = idx
                    break

        # 2. 切 tail（最近 keep_recent 条）
        if cfg.keep_recent <= 0:
            tail: list[Message] = []
            tail_start = len(history_list)
        else:
            tail_start = max(0, len(history_list) - cfg.keep_recent)
            tail = history_list[tail_start:]

        # 3. 中段：跳过 head 所在下标，只看介于 head 和 tail 之间的消息
        middle_source: list[Message] = []
        for idx, msg in enumerate(history_list):
            if head_index is not None and idx == head_index:
                continue
            if idx >= tail_start:
                break
            middle_source.append(msg)
        middle = self._filter_middle(middle_source)

        combined = head + middle + tail
        # 防御：极端参数下仍保证至少是 tail，不返回空。
        return combined if combined else tail

    def _filter_middle(self, middle: Sequence[Message]) -> list[Message]:
        """保留有实际语义的中段消息，必要时截断 tool 结果。"""
        cfg = self._cfg
        kept: list[Message] = []
        for msg in middle:
            if msg.role == "system":
                # 中段出现的额外 system（比如追加的指令），保留不动。
                kept.append(msg)
                continue
            if msg.role == "tool":
                kept.append(self._truncate_tool_message(msg))
                continue
            # user / assistant：有 content 或 tool_calls 的才留。
            has_content = bool(msg.content and msg.content.strip())
            has_tool_calls = bool(msg.tool_calls)
            if has_content or has_tool_calls:
                kept.append(msg)
        _ = cfg  # 保留 config 上下文引用，避免未来字段扩展时漏接。
        return kept

    def _truncate_tool_message(self, msg: Message) -> Message:
        """单条 tool 结果消息超长时截断 content，保留 metadata。"""
        cfg = self._cfg
        content = msg.content or ""
        limit = cfg.tool_result_max_chars
        if limit <= 0 or len(content) <= limit:
            return msg
        truncated = content[:limit] + _TRUNCATED_SUFFIX
        return Message(
            role=msg.role,
            content=truncated,
            tool_calls=msg.tool_calls,
            tool_call_id=msg.tool_call_id,
            name=msg.name,
            metadata={**msg.metadata, "compactor_truncated": True},
        )


__all__ = [
    "CompactorConfig",
    "HistoryCompactor",
]
