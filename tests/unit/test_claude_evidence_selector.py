"""claude_evidence_selector 单测。

覆盖 dev-checklist #3 happy path / #4 过滤逻辑 / #5 异常路径。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from evolution.claude_evidence_selector import build_claude_transcript_window

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "claude_jsonl"
THREAD_ID = "thread-test000001"
CLAUDE_THREAD_ID = "test-claude-session-001"
CWD = "test-cwd"
RUN_ID = "run-claude-thread-test000001-5"


def _setup_fixture(claude_home: Path, fixture_name: str) -> None:
    """Copy a fixture jsonl into the claude_home directory structure."""
    from web.integrations.claude_code.jsonl_history import encode_cwd

    encoded = encode_cwd(CWD)
    target_dir = claude_home / "projects" / encoded
    target_dir.mkdir(parents=True, exist_ok=True)
    src = FIXTURE_DIR / fixture_name
    dst = target_dir / f"{CLAUDE_THREAD_ID}.jsonl"
    if dst.exists():
        dst.unlink()
    shutil.copy2(src, dst)


def _build(claude_home: Path, max_messages: int = 100) -> object:
    return build_claude_transcript_window(
        thread_id=THREAD_ID,
        claude_thread_id=CLAUDE_THREAD_ID,
        cwd=CWD,
        run_id=RUN_ID,
        max_messages=max_messages,
        claude_home=claude_home,
    )


# ---------------------------------------------------------------------------
# #3 Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_clean_5_turns(self, tmp_path: Path) -> None:
        _setup_fixture(tmp_path, "clean_5_turns.jsonl")
        window = _build(tmp_path)

        assert len(window.messages) == 10
        assert window.user_turn_count == 5
        assert window.session_id == THREAD_ID
        assert window.run_id == RUN_ID
        assert window.tool_call_count == 0
        assert window.final_message is None

        turns = [m.turn for m in window.messages]
        assert turns == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]

        roles = [m.role for m in window.messages]
        assert roles == ["user", "assistant"] * 5

        assert window.included_turns == (1, 2, 3, 4, 5)
        assert window.summary is not None
        assert "10 claude messages across 5 turns" in window.summary

    def test_single_user_no_assistant(self, tmp_path: Path) -> None:
        _setup_fixture(tmp_path, "single_user_no_assistant.jsonl")
        window = _build(tmp_path)

        assert len(window.messages) == 1
        assert window.messages[0].role == "user"
        assert window.messages[0].turn == 1
        assert window.messages[0].content == "这是一个没有回复的问题"
        assert window.user_turn_count == 1
        assert window.included_turns == (1,)

    def test_max_messages_truncation(self, tmp_path: Path) -> None:
        """clean_5_turns 有 10 条 text，截到 max_messages=5 应该只保留最后 5 条。"""
        _setup_fixture(tmp_path, "clean_5_turns.jsonl")
        window = _build(tmp_path, max_messages=5)

        assert len(window.messages) == 5

        # 最后 5 条是：assistant"第三个回答", user"第四个问题", assistant"第四个回答",
        #              user"第五个问题", assistant"第五个回答"
        # turn 递增：assistant→0, user→1, assistant→1, user→2, assistant→2
        turns = [m.turn for m in window.messages]
        assert turns == [0, 1, 1, 2, 2]
        assert window.user_turn_count == 2
        # turn 0 不进 included_turns（turn > 0）
        assert window.included_turns == (1, 2)

    def test_turn_pairing(self, tmp_path: Path) -> None:
        """验证 assistant 消息跟前一个 user 共享同一个 turn。"""
        _setup_fixture(tmp_path, "clean_5_turns.jsonl")
        window = _build(tmp_path)

        for msg in window.messages:
            assert msg.tool_name is None

        # 每对 (user, assistant) 的 turn 相同
        for i in range(0, len(window.messages), 2):
            user_msg = window.messages[i]
            assistant_msg = window.messages[i + 1]
            assert user_msg.role == "user"
            assert assistant_msg.role == "assistant"
            assert user_msg.turn == assistant_msg.turn


# ---------------------------------------------------------------------------
# #4 过滤逻辑
# ---------------------------------------------------------------------------


class TestFilterLogic:
    def test_with_tool_use_filtered(self, tmp_path: Path) -> None:
        """tool_use 和 tool_result 被过滤，只保留 kind=="text"。"""
        _setup_fixture(tmp_path, "with_tool_use.jsonl")
        window = _build(tmp_path)

        # 5 条 text：user + assistant + assistant + user + assistant
        assert len(window.messages) == 5
        assert all(m.role in ("user", "assistant") for m in window.messages)
        assert window.user_turn_count == 2

        roles = [m.role for m in window.messages]
        assert roles == ["user", "assistant", "assistant", "user", "assistant"]

        turns = [m.turn for m in window.messages]
        assert turns == [1, 1, 1, 2, 2]

    def test_with_thinking_filtered(self, tmp_path: Path) -> None:
        """thinking block 被过滤，只保留 kind=="text"。"""
        _setup_fixture(tmp_path, "with_thinking.jsonl")
        window = _build(tmp_path)

        # 6 条 text：3 user + 3 assistant
        assert len(window.messages) == 6
        assert window.user_turn_count == 3

        roles = [m.role for m in window.messages]
        assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
        assert window.included_turns == (1, 2, 3)


# ---------------------------------------------------------------------------
# #5 异常路径
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_malformed_line_skipped(self, tmp_path: Path) -> None:
        """坏 JSON 行被 jsonl_history 跳过，剩余有效行正常解析。"""
        _setup_fixture(tmp_path, "malformed_line.jsonl")
        window = _build(tmp_path)

        # 4 条 text（坏行被跳过）
        assert len(window.messages) == 4
        assert window.user_turn_count == 2
        turns = [m.turn for m in window.messages]
        assert turns == [1, 1, 2, 2]

    def test_wrong_session_id_skipped(self, tmp_path: Path) -> None:
        """sessionId 不匹配的行被 parse_jsonl_history 跳过。"""
        _setup_fixture(tmp_path, "wrong_session_id.jsonl")
        window = _build(tmp_path)

        assert len(window.messages) == 1
        assert window.messages[0].role == "user"
        assert window.messages[0].content == "匹配的消息"
        assert window.messages[0].turn == 1

    def test_empty_jsonl(self, tmp_path: Path) -> None:
        """空 jsonl 文件返回空 window + 'empty jsonl' summary。"""
        _setup_fixture(tmp_path, "empty.jsonl")
        window = _build(tmp_path)

        assert len(window.messages) == 0
        assert window.user_turn_count == 0
        assert window.summary == "empty jsonl"
        assert window.session_id == THREAD_ID
        assert window.run_id == RUN_ID

    def test_file_not_found(self, tmp_path: Path) -> None:
        """jsonl 文件不存在时返回空 window + 'jsonl not found' summary，不抛异常。"""
        # 不设置任何 fixture — 文件不存在
        window = _build(tmp_path)

        assert len(window.messages) == 0
        assert window.summary == "jsonl not found"
        assert window.session_id == THREAD_ID
        assert window.run_id == RUN_ID
        assert window.tool_call_count == 0
        assert window.final_message is None
