"""TranscriptProvider Protocol — 频道无关的对话历史读取抽象。"""

from __future__ import annotations

from typing import Protocol

from evolution.models import TranscriptWindow

__all__ = ["TranscriptProvider"]


class TranscriptProvider(Protocol):
    """读对话历史的抽象。每个频道实现一份。

    EvolutionManager 只通过本 Protocol 消费对话数据，
    不知道底层是 claude jsonl / codex jsonl / native session history。
    """

    @property
    def channel_id(self) -> str:
        """频道标识，如 ``"claude"`` / ``"codex"`` / ``"native"``。

        用于：run_id 拼接、日志、事件 metadata。
        """
        ...

    async def build_window(
        self,
        *,
        run_id: str,
        max_messages: int,
    ) -> TranscriptWindow:
        """读对话历史并拍平。失败返回空 messages 的 window，不抛。"""
        ...
