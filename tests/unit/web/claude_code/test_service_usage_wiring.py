"""ClaudeCodeService usage wiring 单测：验证 record_run_usage 在主循环 complete 时被调用。

覆盖 3 个场景：
1. 完整流程——thread_manager 存在 + 有效 thread_id → 调 record_run_usage
2. thread_manager=None → 跳过
3. placeholder register_id（pending-XXX）→ 跳过
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

from web._shared.session_manager import SessionManager
from web.claude_code.approval import ApprovalBridge
from web.claude_code.normalizer import ClaudeNormalizer
from web.claude_code.service import ClaudeCodeService


class _FakeWriter:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)


def _make_fake_client(messages: list[Any]) -> MagicMock:
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()

    async def receive_response() -> Any:
        for m in messages:
            yield m

    client.receive_response = receive_response
    client.interrupt = AsyncMock()
    return client


def _make_result(usage: dict[str, Any] | None = None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        session_id="sid",
        stop_reason="end_turn",
        total_cost_usd=0.01,
        usage=usage or {"input_tokens": 10, "output_tokens": 5},
        result="ok",
    )


def _make_assistant(text: str = "hi") -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude",
        parent_tool_use_id=None,
        message_id="msg-1",
    )


def _make_fake_usage_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.record_run_usage = AsyncMock(return_value=MagicMock())
    return mgr


def _make_fake_thread_manager(usage_manager: MagicMock) -> MagicMock:
    tm = MagicMock()
    tm.usage_manager = usage_manager
    tm.list_threads = MagicMock(return_value=[])
    return tm


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_run_usage_called_on_complete() -> None:
    """完整流程：有效 thread_id + thread_manager → record_run_usage 被调用。"""
    usage_data = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 20}
    msgs = [_make_assistant("hello"), _make_result(usage=usage_data)]
    fake_client = _make_fake_client(msgs)

    usage_mgr = _make_fake_usage_manager()
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(
        normalizer,
        approval,
        sessions,
        client_factory=lambda **_: fake_client,
        thread_manager=thread_mgr,
    )

    writer = _FakeWriter()
    # register_id_override 使用有效 thread_id 格式
    await svc.query(
        "hi",
        {"sessionId": "sid-1"},
        writer,
        register_id_override="thread-aabbccddeeff",
    )

    # 断言 record_run_usage 被调用
    usage_mgr.record_run_usage.assert_awaited_once()
    call_kwargs = usage_mgr.record_run_usage.call_args
    # 第一个位置参数是 thread_id
    assert call_kwargs[0][0] == "thread-aabbccddeeff"
    # keyword args
    assert call_kwargs[1]["channel"] == "anthropic"
    assert call_kwargs[1]["raw_payload"] == usage_data
    assert call_kwargs[1]["turn"] == 1
    assert call_kwargs[1]["run_id"] == "thread-aabbccddeeff-1"


@pytest.mark.asyncio
async def test_record_run_usage_skipped_when_thread_manager_none() -> None:
    """thread_manager=None → 不调 record_run_usage。"""
    msgs = [_make_result()]
    fake_client = _make_fake_client(msgs)

    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(
        normalizer,
        approval,
        sessions,
        client_factory=lambda **_: fake_client,
        thread_manager=None,
    )

    writer = _FakeWriter()
    await svc.query(
        "hi",
        {"sessionId": "sid-1"},
        writer,
        register_id_override="thread-aabbccddeeff",
    )

    # 没有 thread_manager → 不会调到 usage_manager
    # 如果 service 试图访问 None.usage_manager 会抛 AttributeError
    # 测试能跑完就说明跳过逻辑正确
    kinds = [m.get("kind") for m in writer.sent]
    assert "complete" in kinds


@pytest.mark.asyncio
async def test_record_run_usage_skipped_for_placeholder_id() -> None:
    """register_id 是 placeholder（非 thread-<12hex>）→ 跳过。"""
    usage_data = {"input_tokens": 10, "output_tokens": 5}
    msgs = [_make_result(usage=usage_data)]
    fake_client = _make_fake_client(msgs)

    usage_mgr = _make_fake_usage_manager()
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(
        normalizer,
        approval,
        sessions,
        client_factory=lambda **_: fake_client,
        thread_manager=thread_mgr,
    )

    writer = _FakeWriter()
    # 不传 register_id_override → register_id 是 sessionId（非 thread-xxx 格式）
    await svc.query("hi", {"sessionId": "sid-1"}, writer)

    # placeholder 不是 thread-<12hex> → 跳过
    usage_mgr.record_run_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_run_usage_increments_run_counter() -> None:
    """多次 query 调用 run_index 递增。"""
    usage_data = {"input_tokens": 10, "output_tokens": 5}
    usage_mgr = _make_fake_usage_manager()
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)

    def make_client(**_: Any) -> MagicMock:
        return _make_fake_client([_make_result(usage=usage_data)])

    svc = ClaudeCodeService(
        normalizer,
        approval,
        sessions,
        client_factory=make_client,
        thread_manager=thread_mgr,
    )

    writer = _FakeWriter()
    tid = "thread-aabbccddeeff"

    await svc.query("run1", {"sessionId": "s1"}, writer, register_id_override=tid)
    await svc.query("run2", {"sessionId": "s2"}, writer, register_id_override=tid)

    assert usage_mgr.record_run_usage.await_count == 2
    # 第一次 turn=1, run_id=thread-aabbccddeeff-1
    first_call = usage_mgr.record_run_usage.call_args_list[0]
    assert first_call[1]["turn"] == 1
    assert first_call[1]["run_id"] == f"{tid}-1"
    # 第二次 turn=2, run_id=thread-aabbccddeeff-2
    second_call = usage_mgr.record_run_usage.call_args_list[1]
    assert second_call[1]["turn"] == 2
    assert second_call[1]["run_id"] == f"{tid}-2"


@pytest.mark.asyncio
async def test_record_run_usage_failure_does_not_break_main_flow() -> None:
    """record_run_usage 抛异常不影响主对话流。"""
    usage_data = {"input_tokens": 10, "output_tokens": 5}
    msgs = [_make_assistant("ok"), _make_result(usage=usage_data)]
    fake_client = _make_fake_client(msgs)

    usage_mgr = _make_fake_usage_manager()
    usage_mgr.record_run_usage = AsyncMock(side_effect=RuntimeError("db error"))
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(
        normalizer,
        approval,
        sessions,
        client_factory=lambda **_: fake_client,
        thread_manager=thread_mgr,
    )

    writer = _FakeWriter()
    await svc.query(
        "hi",
        {"sessionId": "sid-1"},
        writer,
        register_id_override="thread-aabbccddeeff",
    )

    # 主流程正常完成——writer 收到 text + complete
    kinds = [m.get("kind") for m in writer.sent]
    assert "text" in kinds
    assert "complete" in kinds
