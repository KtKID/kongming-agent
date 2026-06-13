"""Claude JSONL 证据窗口选择器。

脚本作用：
- 从 Claude Code 的 JSONL 历史文件读取指定会话消息。
- 裁剪最近的纯文本消息，形成 child reviewer 可消费的证据窗口。
- 把 Web 侧 Claude transcript 结构转换为 evolution 模块统一的 TranscriptWindow。
- 在缺文件、空历史或解析异常时返回空窗口，并通过 warning 日志保留诊断信息。

关键函数：
- _empty_window：构造统一的空 TranscriptWindow，供异常路径和空输入复用。
- build_claude_transcript_window：定位并解析 Claude JSONL，筛选文本消息，计算轮次后返回证据窗口。
"""

from __future__ import annotations

import logging
from pathlib import Path

from evolution.models import TranscriptMessage, TranscriptWindow
from hosts.web.integrations.claude_code.jsonl_history import jsonl_path_for, parse_jsonl_history

logger = logging.getLogger(__name__)

__all__ = ["build_claude_transcript_window"]


def _empty_window(
    thread_id: str,
    run_id: str,
    *,
    summary: str | None = None,
) -> TranscriptWindow:
    """构造空证据窗口，保证上游失败时 child reviewer 仍能收到稳定结构。"""
    return TranscriptWindow(
        session_id=thread_id,
        run_id=run_id,
        user_turn_count=0,
        included_turns=(),
        messages=(),
        final_message=None,
        tool_call_count=0,
        summary=summary,
    )


def build_claude_transcript_window(
    *,
    thread_id: str,
    claude_thread_id: str,
    cwd: str,
    run_id: str,
    max_messages: int,
    claude_home: Path | None = None,
) -> TranscriptWindow:
    """读取 Claude JSONL 历史，并拍平成 evolution 使用的 TranscriptWindow。

    关键流程：
    - 根据 cwd、claude_thread_id 和 claude_home 定位 JSONL 文件。
    - 复用 jsonl_history 只读 parser 解析历史消息。
    - 保留纯文本消息，按 max_messages 裁剪最近上下文。
    - 按 user 消息递增 turn，生成 reviewer 证据窗口。
    - 异常路径返回空 messages 的 TranscriptWindow，并写 warning 日志。
    """
    try:
        # 根据 Claude 工作区、线程 ID 和可选 home 目录定位原始 JSONL 历史文件。
        path = jsonl_path_for(cwd, claude_thread_id, claude_home)

        if not path.exists():
            logger.warning("jsonl not found: %s", path)
            return _empty_window(thread_id, run_id, summary="jsonl not found")

        # 解析器负责处理 Claude JSONL 的多种消息形态，这里只消费标准化后的条目。
        entries = parse_jsonl_history(path, claude_thread_id)

        # 只保留 parser 标准化为 frame_type == "text" 的纯文本消息。
        text_entries = [e for e in entries if e.get("frame_type") == "text"]

        if max_messages <= 0 or not text_entries:
            return _empty_window(thread_id, run_id, summary="empty jsonl")

        # 从尾部裁剪最近 max_messages 条，避免 reviewer 输入过长。
        text_entries = text_entries[-max_messages:]

        transcript: list[TranscriptMessage] = []
        current_turn = 0
        for entry in text_entries:
            role = entry.get("role", "")
            if role == "user":
                current_turn += 1
            # TranscriptWindow 使用 turn + role + content 的稳定结构，工具调用信息在当前选择器中留空。
            transcript.append(
                TranscriptMessage(
                    turn=current_turn,
                    role=role,
                    content=entry.get("content", ""),
                    tool_name=None,
                )
            )

        # included_turns 只记录有效用户轮次，summary 给日志和 reviewer 调试使用。
        user_turn_count = sum(1 for m in transcript if m.role == "user")
        included_turns = tuple(sorted(set(m.turn for m in transcript if m.turn > 0)))
        summary = f"{len(transcript)} claude messages across {len(included_turns)} turns"

        return TranscriptWindow(
            session_id=thread_id,
            run_id=run_id,
            user_turn_count=user_turn_count,
            included_turns=included_turns,
            messages=tuple(transcript),
            final_message=None,
            tool_call_count=0,
            summary=summary,
        )
    except Exception as exc:
        logger.warning("build_claude_transcript_window failed: %s", exc)
        return _empty_window(thread_id, run_id, summary=f"error: {exc}")
