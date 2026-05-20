"""MockTranscriptProvider — task-2 频道无关性验证用。

D9 要求：用 MockTranscriptProvider 跑完整流程，
测试文件内不出现 "claude"/"codex"/"native" 字样（除文件名/注释）。
"""

from __future__ import annotations

from dataclasses import dataclass

from evolution.models import TranscriptMessage, TranscriptWindow


@dataclass(frozen=True)
class MockTranscriptProvider:
    """测试用 TranscriptProvider 实现，返回预制 TranscriptWindow。"""

    thread_id: str
    _channel: str = "mock"
    _messages: tuple[TranscriptMessage, ...] = ()

    @property
    def channel_id(self) -> str:
        return self._channel

    async def build_window(
        self,
        *,
        run_id: str,
        max_messages: int,
    ) -> TranscriptWindow:
        messages = self._messages[-max_messages:] if max_messages > 0 else ()
        user_count = sum(1 for m in messages if m.role == "user")
        turns = tuple(sorted(set(m.turn for m in messages if m.turn > 0)))
        return TranscriptWindow(
            session_id=self.thread_id,
            run_id=run_id,
            user_turn_count=user_count,
            included_turns=turns,
            messages=messages,
            final_message=None,
            tool_call_count=0,
            summary=f"{len(messages)} mock messages across {len(turns)} turns",
        )
