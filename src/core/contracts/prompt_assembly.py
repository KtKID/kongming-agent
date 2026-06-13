"""Prompt assembly protocols and value objects."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from core.message import Message

# MessageCompactor
# ---------------------------------------------------------------------------


@runtime_checkable
class MessageCompactor(Protocol):
    """runner 在每个 turn 把 history 送给 LLM 之前的加工钩子。

    实现类在 ``prompting.compaction.history_compactor.HistoryCompactor``；core 只定义接口，
    不持有实现。命名刻意和实现类错开（Protocol = ``MessageCompactor``，实现 =
    ``HistoryCompactor``），避免 ``from core.contracts import HistoryCompactor``
    和 ``from prompting import HistoryCompactor`` 同名歧义。

    典型实现：压缩超长 history（裁剪空白消息、截断长 tool_result）。未来如果要做
    敏感字段 redact / few-shot 注入，也走同一个 Protocol，不新增协议。
    """

    async def compact(self, history: Sequence[Message]) -> list[Message]:
        """给定原始 history，返回加工后的 messages 列表。

        约定：
        - 永远返回**新** list，不就地修改入参
        - 空输入 → 空 list
        - 不涉及阈值时可以直接返回 ``list(history)``（原样拷贝）
        """
        ...


# ---------------------------------------------------------------------------
# InputAssembler 协议
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssembledInput:
    """InputAssembler.assemble() 的返回类型契约。

    core 在此定义完整数据结构（协议单一真源），prompting 层 InputAssembler
    直接使用此类型，不再重定义。

    Attributes:
        messages: 要发给 provider 的完整 messages（system 在最前，compact 后）。
        metadata: 装配元数据，runner 用于构造 ``history.compact`` 事件。
            最少含 ``original_count`` 和 ``compacted_count`` 两个 int 字段。
        system_message: 本次新追加的 system 消息；``None`` 表示没有追加。
    """

    messages: list[Message]
    metadata: dict[str, Any] = field(default_factory=dict)
    system_message: Message | None = None


class PromptSource(Protocol):
    """指令来源的最小 Protocol，runner 用于传给 assembler。

    具体实现（``prompting.instructions.instruction_loader.InstructionSource``）是 frozen
    dataclass，满足此 Protocol。runner 只需知道 origin 和 content 两个属性。
    """

    @property
    def origin(self) -> str: ...

    @property
    def content(self) -> str: ...


class PromptAssembler(Protocol):
    """prompt build 的统一入口协议（runner 对 InputAssembler 的视图）。

    实现类在 ``prompting.assembly.input_assembler.InputAssembler``；core 只定义接口，
    不持有实现。runner 只调用 :meth:`assemble`，不关心内部如何做 compact /
    system 注入。

    注意：返回值类型声明为 :class:`AssembledInput`（core 定义的最小契约），
    实现方可以返回更丰富的子类型（例如 prompting 里带 ``system_message`` 字段
    的版本），runner 只读 ``messages`` 和 ``metadata``，不受影响。
    """

    async def assemble(
        self,
        history: Sequence[Message],
        instructions: Sequence[Any] = (),
    ) -> AssembledInput:
        """装配最终输入。

        Args:
            history: 当前 session 的原始历史。
            instructions: 静态指令来源列表；空列表则不注入 system。

        Returns:
            满足 :class:`AssembledInput` 结构的装配结果（``messages`` + ``metadata``）。
        """
        ...


class PromptDebugSink(Protocol):
    """prompt debug dump 输出协议。

    实现方通常在 ``infrastructure.tracing/``，runner 只负责在 LLM request 前把
    当前 turn 的 prompt build 快照交出去。
    """

    def dump(
        self,
        *,
        session_id: str,
        run_id: str,
        turn: int,
        model: str,
        instruction_origins: Sequence[str],
        history_before_assemble: Sequence[Message],
        assembled_messages: Sequence[Message],
        metadata: Mapping[str, Any],
        added_system_prompt: str | None,
    ) -> str:
        """写出 prompt debug 快照，返回输出路径字符串。"""
        ...


__all__ = [
    "AssembledInput",
    "MessageCompactor",
    "PromptAssembler",
    "PromptDebugSink",
    "PromptSource",
]
