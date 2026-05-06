"""codex jsonl_history.py 单测。

覆盖边界用例：
- 用例 11: event_msg 以外的行跳过（session_meta / response_item / turn_context）
- 用例 12: response_item 跳过
- 用例 13: session_meta 跳过
- 用例 22: session_meta.cwd 优先（read_session_meta）
- 用例 23: 不完整 rollout 不抛错
"""

from __future__ import annotations

import shutil
from pathlib import Path

from web.codex.jsonl_history import parse_codex_rollout, read_session_meta

_FIXTURE_DIR = Path(__file__).parent.parent.parent.parent / "fixtures" / "codex"


def _copy_fixture(name: str, dest_dir: Path) -> Path:
    src = _FIXTURE_DIR / name
    dst = dest_dir / name
    shutil.copy(src, dst)
    return dst


# ---------------------------------------------------------------------------
# TestParseMinimal — 用例 13 + 12
# ---------------------------------------------------------------------------


class TestParseMinimal:
    """用例 13: session_meta 跳过；用例 12: response_item 跳过。"""

    def test_skips_session_meta_and_response_item(self, tmp_path: Path) -> None:
        path = _copy_fixture("rollout-minimal.jsonl", tmp_path)
        sid = "019dee38-c689-7341-9011-d8cd06e8aeca"

        msgs = parse_codex_rollout(path, sid)

        # session_meta / response_item / turn_context / task_started /
        # task_complete 都不应出现在结果里
        kinds = {m["kind"] for m in msgs}
        assert "session_created" not in kinds
        assert "response_item" not in kinds
        # 不应含 task_started / task_complete 衍生内容
        for m in msgs:
            content = m.get("content", "")
            if isinstance(content, str):
                assert "session_meta" not in content
                assert "response_item" not in content

    def test_agent_message_translated(self, tmp_path: Path) -> None:
        path = _copy_fixture("rollout-minimal.jsonl", tmp_path)
        sid = "019dee38-c689-7341-9011-d8cd06e8aeca"

        msgs = parse_codex_rollout(path, sid)

        text_msgs = [m for m in msgs if m["kind"] == "text"]
        assert len(text_msgs) == 1
        assert text_msgs[0]["content"] == "hello from codex"
        assert text_msgs[0]["role"] == "assistant"
        assert text_msgs[0]["sessionId"] == sid
        assert text_msgs[0]["provider"] == "codex"


# ---------------------------------------------------------------------------
# TestToolCallRollout
# ---------------------------------------------------------------------------


class TestToolCallRollout:
    def test_command_execution_translated(self, tmp_path: Path) -> None:
        path = _copy_fixture("rollout-with-tool-call.jsonl", tmp_path)
        sid = "019dee40-aaaa-7341-9011-aabbccddeeff"

        msgs = parse_codex_rollout(path, sid)

        tool_use_msgs = [m for m in msgs if m["kind"] == "tool_use"]
        tool_result_msgs = [m for m in msgs if m["kind"] == "tool_result"]
        # command_execution 产出 tool_use + tool_result
        assert any(m["toolName"] == "shell" for m in tool_use_msgs)
        assert len(tool_result_msgs) >= 1

    def test_mcp_tool_call_translated(self, tmp_path: Path) -> None:
        path = _copy_fixture("rollout-with-tool-call.jsonl", tmp_path)
        sid = "019dee40-aaaa-7341-9011-aabbccddeeff"

        msgs = parse_codex_rollout(path, sid)

        tool_use_msgs = [m for m in msgs if m["kind"] == "tool_use"]
        mcp_uses = [m for m in tool_use_msgs if "test-server" in m.get("toolName", "")]
        assert len(mcp_uses) == 1
        assert mcp_uses[0]["toolName"] == "test-server/search"


# ---------------------------------------------------------------------------
# TestPartialRollout — 用例 23
# ---------------------------------------------------------------------------


class TestPartialRollout:
    """用例 23: 不完整 rollout 不抛错。"""

    def test_aborted_rollout_no_error(self, tmp_path: Path) -> None:
        path = _copy_fixture("rollout-aborted.jsonl", tmp_path)
        sid = "019dee50-bbbb-7341-9011-112233445566"

        # 不抛 EOFError / ValueError
        msgs = parse_codex_rollout(path, sid)

        assert isinstance(msgs, list)
        # 至少有 agent_message "Starting work..."
        text_msgs = [m for m in msgs if m["kind"] == "text"]
        assert len(text_msgs) == 1
        assert text_msgs[0]["content"] == "Starting work..."


# ---------------------------------------------------------------------------
# TestCwdTruth — 用例 22
# ---------------------------------------------------------------------------


class TestCwdTruth:
    """用例 22: session_meta.cwd 优先。"""

    def test_read_session_meta_returns_cwd(self, tmp_path: Path) -> None:
        path = _copy_fixture("rollout-minimal.jsonl", tmp_path)

        meta = read_session_meta(path)

        assert meta is not None
        assert meta["cwd"] == "/test/path/proj"
        assert meta["id"] == "019dee38-c689-7341-9011-d8cd06e8aeca"

    def test_read_session_meta_missing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "nonexistent.jsonl"

        result = read_session_meta(path)

        assert result is None


# ---------------------------------------------------------------------------
# TestTimestampOrder
# ---------------------------------------------------------------------------


class TestTimestampOrder:
    def test_messages_sorted_by_timestamp(self, tmp_path: Path) -> None:
        path = _copy_fixture("rollout-minimal.jsonl", tmp_path)
        sid = "019dee38-c689-7341-9011-d8cd06e8aeca"

        msgs = parse_codex_rollout(path, sid)

        timestamps = [m["timestamp"] for m in msgs]
        assert timestamps == sorted(timestamps)
        assert len(timestamps) >= 1
