"""把历史、指令、（预留的）附件引用装配成模型真正看到的输入。

v1-mini 的装配职责：

1. 把 :class:`context.instruction_loader.InstructionSource` 渲染成一条 ``system``
   消息（若历史里还没 system）
2. 调用 :class:`context.history_compactor.HistoryCompactor` 做历史裁剪
3. 把 system + 压缩后 history 合并成最终的 messages 序列
4. 在 metadata 里记下原始 / 压缩后长度、是否追加了 system，便于 trace 和调试

**刻意不做**的事：

- 不拼 token 精算：交给 provider 或后续 context_engine（v0.2+）
- 不处理附件 / 图像 / 文件引用：v1-mini 没有 attachment 概念，只在 metadata 里留
  ``attachments`` 占位字段，方便上层未来扩展时不用改签名
- 不改变任何消息内容（除非 compactor 自己截断了 tool 结果）

返回的 :class:`AssembledInput` 是 frozen dataclass，只读；调用方要追加字段自己拿
dict 拷一份。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from context.history_compactor import HistoryCompactor
from context.instruction_loader import InstructionLoader, InstructionSource
from core.message import Message


@dataclass(frozen=True)
class AssembledInput:
    """装配结果。

    Attributes:
        system_message: 本次调用新追加的 system 消息；``None`` 表示没有追加
            （历史里已经有 system，或 sources 为空）。
        messages: 要发给 provider 的完整 messages，顺序已正确（system 在最前）。
        metadata: 装配过程的统计 / 诊断字段。v1-mini 固定含：

            - ``original_count`` (int): 传入 history 的原始长度
            - ``compacted_count`` (int): 压缩后剩余条数
            - ``added_system`` (bool): 是否追加了新 system 消息
            - ``instruction_sources`` (list[str]): 参与渲染的 source origin 名
            - ``attachments`` (list): 占位字段，v1-mini 永远是空 list
    """

    system_message: Message | None
    messages: list[Message]
    metadata: dict[str, Any] = field(default_factory=dict)


class InputAssembler:
    """最小输入装配器。"""

    def __init__(
        self,
        *,
        compactor: HistoryCompactor | None = None,
    ) -> None:
        """初始化。

        Args:
            compactor: 历史压缩器；未传则用默认参数的 :class:`HistoryCompactor`。
        """
        self._compactor = compactor or HistoryCompactor()

    @property
    def compactor(self) -> HistoryCompactor:
        """暴露给上层做 trace 打点用。"""
        return self._compactor

    async def assemble(
        self,
        history: Sequence[Message],
        instructions: Sequence[InstructionSource] = (),
        *,
        instruction_loader: InstructionLoader | None = None,
    ) -> AssembledInput:
        """装配最终输入。

        Args:
            history: 当前 session 的历史消息。不就地修改。
            instructions: 已加载好的指令来源列表（通常由调用方先跑
                ``InstructionLoader.load(...)`` 得到）。
            instruction_loader: 可选，自定义渲染器；未传时新建一个默认的。注意
                ``load`` 操作**不在**这里跑——只借用它的 :meth:`render` 方法，
                避免装配阶段再做 I/O。

        Returns:
            :class:`AssembledInput`。
        """
        loader = instruction_loader or InstructionLoader()
        system_text = loader.render(instructions) if instructions else ""
        has_existing_system = any(m.role == "system" for m in history)

        system_message: Message | None = None
        if system_text and not has_existing_system:
            system_message = Message.system(system_text)

        compacted = await self._compactor.compact(history)

        final_messages: list[Message] = []
        if system_message is not None:
            final_messages.append(system_message)
        final_messages.extend(compacted)

        metadata: dict[str, Any] = {
            "original_count": len(history),
            "compacted_count": len(compacted),
            "added_system": system_message is not None,
            "instruction_sources": [s.origin for s in instructions],
            "attachments": [],  # 占位，v1-mini 不处理。
        }
        return AssembledInput(
            system_message=system_message,
            messages=final_messages,
            metadata=metadata,
        )


__all__ = [
    "AssembledInput",
    "InputAssembler",
]
