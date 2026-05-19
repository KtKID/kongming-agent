"""inbox_helpers 单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from safety.inbox_helpers import emit_remove_safe, to_inbox_payload


class TestToInboxPayload:
    """to_inbox_payload 字段映射测试。"""

    def test_generic_chat_minimal_fields(self) -> None:
        """generic_chat 通道：autoApprove/autoReject/blockedByRule 全 None。"""
        payload = to_inbox_payload(
            channel="generic_chat",
            thread_id="t1",
            request_id="r1",
            tool_name="Bash",
            tool_input={"command": "ls"},
            cwd="/tmp",
            arrived_at_ms=1234,
            timeout_ms=60_000,
        )
        assert payload["channel"] == "generic_chat"
        assert payload["threadId"] == "t1"
        assert payload["requestId"] == "r1"
        assert payload["toolName"] == "Bash"
        assert payload["toolInput"] == {"command": "ls"}
        assert payload["autoApproveAtMs"] is None
        assert payload["autoRejectAtMs"] is None
        assert payload["blockedByRule"] is None
        assert payload["isElevated"] is False  # standard
        assert payload["cwd"] == "/tmp"
        assert payload["arrivedAtMs"] == 1234
        assert "kind" not in payload  # kind 由 broadcaster 自动补

    def test_claude_code_full_fields(self) -> None:
        """claude_code 通道（阶段 2 将调用）：所有字段都填。"""
        payload = to_inbox_payload(
            channel="claude_code",
            thread_id="thread-x",
            request_id="req-y",
            tool_name="Bash",
            tool_input={"command": "rm -rf x"},
            cwd="/usr/foo",
            arrived_at_ms=5000,
            timeout_ms=120_000,
            severity="elevated",
            auto_reject_at_ms=35_000,
            blocked_by_rule="Bash(rm:*)",
        )
        assert payload["isElevated"] is True
        assert payload["autoRejectAtMs"] == 35_000
        assert payload["blockedByRule"] == "Bash(rm:*)"
        assert payload["autoApproveAtMs"] is None  # 危险路径只有 reject 倒计时

    def test_severity_elevated_maps_to_is_elevated_true(self) -> None:
        """severity='elevated' → isElevated=True；'standard' → False。"""
        elev = to_inbox_payload(
            channel="generic_chat",
            thread_id="t",
            request_id="r",
            tool_name="X",
            tool_input={},
            cwd="/",
            arrived_at_ms=0,
            timeout_ms=60_000,
            severity="elevated",
        )
        std = to_inbox_payload(
            channel="generic_chat",
            thread_id="t",
            request_id="r",
            tool_name="X",
            tool_input={},
            cwd="/",
            arrived_at_ms=0,
            timeout_ms=60_000,
            severity="standard",
        )
        assert elev["isElevated"] is True
        assert std["isElevated"] is False

    def test_timeout_ms_passthrough(self) -> None:
        """timeout_ms 入参原样写入 payload.timeoutMs 字段（前端 fallback 用）。

        覆盖 smart-approval-generic-chat-autoallow 任务 #1 P0 约束：
        前端在 autoApprove/autoReject 均 None 时，按 arrivedAtMs + timeoutMs
        本地推算到点。
        """
        payload = to_inbox_payload(
            channel="generic_chat",
            thread_id="t",
            request_id="r",
            tool_name="Bash",
            tool_input={"command": "ls"},
            cwd="/tmp",
            arrived_at_ms=1000,
            timeout_ms=45_000,
        )
        assert payload["timeoutMs"] == 45_000
        assert isinstance(payload["timeoutMs"], int)


class TestEmitRemoveSafe:
    """emit_remove_safe 异常吞掉测试。"""

    @pytest.mark.asyncio
    async def test_success_path(self) -> None:
        """正常路径：broadcaster.emit_remove 被以正确参数调用。"""
        broadcaster = AsyncMock()
        await emit_remove_safe(broadcaster, "r1", "user_decided")
        broadcaster.emit_remove.assert_awaited_once_with("r1", "user_decided")

    @pytest.mark.asyncio
    async def test_swallows_exception_not_raise(self) -> None:
        """异常路径：broadcaster.emit_remove 抛 → emit_remove_safe 不抛，只 warn。"""
        broadcaster = AsyncMock()
        broadcaster.emit_remove.side_effect = RuntimeError("ws closed")
        # 不应抛
        await emit_remove_safe(broadcaster, "r2", "timeout")
        broadcaster.emit_remove.assert_awaited_once()
