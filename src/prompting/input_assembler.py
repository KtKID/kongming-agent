"""把历史、指令、（预留的）附件引用装配成模型真正看到的输入。

v1-mini 的装配职责：

1. 把 :class:`prompting.instruction_loader.InstructionSource` 渲染成一条 ``system``
   消息（若历史里还没 system）
2. 调用 :class:`prompting.history_compactor.HistoryCompactor` 做历史裁剪
3. 把 system + 压缩后 history 合并成最终的 messages 序列
4. 在 metadata 里记下原始 / 压缩后长度、是否追加了 system，便于 trace 和调试

**刻意不做**的事：

- 不拼 token 精算：交给 provider 或后续 context_engine（v0.2+）
- 不在 assembly 阶段读附件 bytes：附件引用透传，bytes 由 provider 通过
  :class:`executors.llm.media_adapter.MediaPart` 的 lazy ``load_bytes()`` 按需读取
- 不改变任何消息内容（除非 compactor 自己截断了 tool 结果）

附件透传（claude-image-paste-e2e §4）：

- 每条 user 消息的 ``Message.metadata["attachments"]`` 自然透传给 provider
  （Message 本身就是装配产物的字段），assembly 不拆解、不还原 MediaPart
- assembly 的 :attr:`AssembledInput.metadata["attachments"]` 是**汇总视图**：
  把本轮所有 user 消息的 attachments 引用拍平到一个 list，便于 trace 观测；
  不参与 provider 输入构造（provider 直接遍历 ``messages[i].metadata``）
- provider 通过 :func:`executors.llm.media_adapter.collect_media_parts_from_messages`
  把 ``messages`` 还原成 ``list[MediaPart]``（解耦：prompting 层不依赖 executors 层）

返回的 :class:`AssembledInput` 是 frozen dataclass，只读；调用方要追加字段自己拿
dict 拷一份。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.contracts import AssembledInput, MessageCompactor
from core.message import Message
from prompting.history_compactor import HistoryCompactor
from prompting.instruction_loader import InstructionLoader, InstructionSource


class InputAssembler:
    """最小输入装配器。"""

    def __init__(
        self,
        *,
        compactor: MessageCompactor | None = None,
    ) -> None:
        """初始化。

        Args:
            compactor: 历史压缩器（满足 MessageCompactor Protocol 即可）；
                未传则用默认参数的 :class:`HistoryCompactor`。
        """
        self._compactor: MessageCompactor = compactor or HistoryCompactor()

    @property
    def compactor(self) -> MessageCompactor:
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

        # 汇总本轮所有 user message 的 attachments 引用，便于 trace 观测。
        # 真正的输入构造由 provider 通过
        # ``executors.llm.media_adapter.collect_media_parts_from_messages``
        # 直接读 ``messages[i].metadata["attachments"]``，不消费此汇总字段——
        # 此字段仅供日志 / trace 看一眼"这一轮带了几张图"。
        attachments_summary: list[dict[str, Any]] = []
        for m in compacted:
            if m.role != "user":
                continue
            refs = (m.metadata or {}).get("attachments")
            if isinstance(refs, list):
                attachments_summary.extend(r for r in refs if isinstance(r, dict))

        metadata: dict[str, Any] = {
            "original_count": len(history),
            "compacted_count": len(compacted),
            "added_system": system_message is not None,
            "instruction_sources": [s.origin for s in instructions],
            "attachments": attachments_summary,
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
