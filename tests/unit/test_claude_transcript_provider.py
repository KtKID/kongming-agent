"""ClaudeTranscriptProvider 单测。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evolution.claude_transcript_provider import ClaudeTranscriptProvider

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "claude_jsonl"
CLAUDE_THREAD_ID = "test-claude-session-001"
CWD = "test-cwd"


def _setup_fixture(claude_home: Path, fixture_name: str) -> None:
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    encoded = encode_cwd(CWD)
    target_dir = claude_home / "projects" / encoded
    target_dir.mkdir(parents=True, exist_ok=True)
    src = FIXTURE_DIR / fixture_name
    dst = target_dir / f"{CLAUDE_THREAD_ID}.jsonl"
    if dst.exists():
        dst.unlink()
    shutil.copy2(src, dst)


class TestClaudeTranscriptProvider:
    def test_channel_id(self) -> None:
        provider = ClaudeTranscriptProvider(
            thread_id="thread-aabbccddeeff",
            claude_thread_id=CLAUDE_THREAD_ID,
            cwd=CWD,
        )
        assert provider.channel_id == "claude"

    @pytest.mark.asyncio
    async def test_build_window_delegates_to_selector(self, tmp_path: Path) -> None:
        _setup_fixture(tmp_path, "clean_5_turns.jsonl")

        provider = ClaudeTranscriptProvider(
            thread_id="thread-aabbccddeeff",
            claude_thread_id=CLAUDE_THREAD_ID,
            cwd=CWD,
            claude_home=tmp_path,
        )

        window = await provider.build_window(
            run_id="run-claude-thread-aabbccddeeff-5",
            max_messages=100,
        )

        assert window.session_id == "thread-aabbccddeeff"
        assert window.run_id == "run-claude-thread-aabbccddeeff-5"
        assert len(window.messages) == 10
        assert window.user_turn_count == 5
