"""完整对话 fork 的 Python ↔ TypeScript REST metadata 合同测试。"""

from __future__ import annotations

import re
from pathlib import Path

from core.message import Message, ToolCall
from hosts.web.app_support.generic_history import normalize_generic_history
from hosts.web.protocol import ForkThreadRequest, ThreadMetadataDTO, TurnEndFrame


def test_thread_metadata_lineage_round_trip_and_typescript_contract() -> None:
    """双侧真源都声明 lineage、精确时间线边界与 schema v13。"""
    parent_id = "thread-111111111111"
    dto = ThreadMetadataDTO(
        id="thread-222222222222",
        name="fork",
        preset_id="preset-a",
        backend_kind="generic_chat",
        forked_from_id=parent_id,
        forked_from_history_index=3,
        created_at=1.0,
        updated_at=2.0,
        message_count=4,
    )
    restored = ThreadMetadataDTO.model_validate_json(dto.model_dump_json())
    assert restored.forked_from_id == parent_id
    assert restored.forked_from_history_index == 3
    assert restored.schema_version == 13

    source = Path("web/src/protocol.ts").read_text(encoding="utf-8")
    match = re.search(
        r"export interface ThreadMetadataDTO \{(?P<body>.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert match is not None
    body = match.group("body")
    assert re.search(r"forked_from_id\?: string \| null;", body)
    assert re.search(r"forked_from_history_index\?: number \| null;", body)
    schema_line = re.search(r"schema_version\?: (?P<versions>[^;]+);", body)
    assert schema_line is not None
    assert "13" in schema_line.group("versions").split(" | ")


def test_reply_fork_boundary_round_trip_and_typescript_contract() -> None:
    """REST、实时 turn.end 与历史回放共享 Session history_index。"""
    assert ForkThreadRequest(history_index=3).model_dump() == {"history_index": 3}
    assert (
        TurnEndFrame(
            timestamp_ms=1,
            turn=2,
            history_index=3,
            has_tool_calls=False,
        ).model_dump()["history_index"]
        == 3
    )

    history = normalize_generic_history(
        [
            Message.user("read"),
            Message.assistant(
                None,
                tool_calls=[
                    ToolCall(
                        call_id="call-1",
                        tool_name="read_file",
                        arguments={"path": "a.txt"},
                    )
                ],
            ),
            Message.tool_result("call-1", "body", name="read_file"),
            Message.assistant("done"),
        ],
        session_id="thread-111111111111",
    )
    assistant = next(
        message
        for message in history
        if message.get("frame_type") == "text" and message.get("role") == "assistant"
    )
    assert assistant["historyIndex"] == 3
    assert all(
        "historyIndex" not in message
        for message in history
        if message.get("frame_type") in {"tool_use", "tool_result"}
    )

    source = Path("web/src/protocol.ts").read_text(encoding="utf-8")
    assert re.search(
        r"export interface ForkThreadRequest \{[^}]*history_index: number;",
        source,
        flags=re.DOTALL,
    )
    assert re.search(
        r"export interface TurnEndFrame \{[^}]*history_index\?: number",
        source,
        flags=re.DOTALL,
    )
    assert re.search(
        r"export interface NormalizedMessage \{[^}]*historyIndex\?: number;",
        source,
        flags=re.DOTALL,
    )
