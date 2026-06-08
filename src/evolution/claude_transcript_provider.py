"""ClaudeTranscriptProvider — claude 频道的 TranscriptProvider 实现。

内部调 ``claude_evidence_selector.build_claude_transcript_window`` 读 jsonl。
是 evolution 模块向 ``web.integrations.claude_code.jsonl_history`` 借数据的唯一桥。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from evolution.claude_evidence_selector import build_claude_transcript_window
from evolution.models import TranscriptWindow

__all__ = ["ClaudeTranscriptProvider"]


@dataclass(frozen=True)
class ClaudeTranscriptProvider:
    """claude 频道的 TranscriptProvider 实现（duck typing）。"""

    thread_id: str
    claude_thread_id: str
    cwd: str
    claude_home: Path | None = None

    @property
    def channel_id(self) -> str:
        return "claude"

    async def build_window(
        self,
        *,
        run_id: str,
        max_messages: int,
    ) -> TranscriptWindow:
        return build_claude_transcript_window(
            thread_id=self.thread_id,
            claude_thread_id=self.claude_thread_id,
            cwd=self.cwd,
            run_id=run_id,
            max_messages=max_messages,
            claude_home=self.claude_home,
        )
