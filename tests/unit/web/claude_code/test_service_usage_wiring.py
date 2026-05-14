"""ClaudeCodeService usage wiring 单测。

⚠️ **v0.1 改动（usage-token-derive-from-jsonl）**：

claude_code 通道彻底走 SDK jsonl 派生路径，``_consume`` 主循环里 **不再调
``record_run_usage``**：

- ``AssistantMessage`` → ``set_last_assistant_usage``（**纯内存**）+ emit
  ``usage_summary_updated`` WS 帧给前端实时刷
- ``ResultMessage (complete)`` → 只 emit ``usage_summary_updated`` WS 帧
  （manager 内部走派生从 jsonl 算最新 usage），**不再写盘**

防回归用例：本套测试钉死"complete 路径不再调 record_run_usage"。
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


def _make_assistant(
    text: str = "hi",
    *,
    model: str = "claude",
    usage: dict[str, Any] | None = None,
) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model=model,
        parent_tool_use_id=None,
        message_id="msg-1",
        usage=usage,
    )


def _make_fake_usage_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.record_run_usage = AsyncMock(return_value=MagicMock())
    mgr.set_last_assistant_usage = MagicMock(return_value=MagicMock())
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
async def test_record_run_usage_NOT_called_on_complete() -> None:
    """**v0.1 防回归**：complete 路径**不再调** record_run_usage。

    claude_code 通道走 SDK jsonl 派生，service 层零写盘调用。
    """
    usage_data = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 20}
    msgs = [_make_assistant("hello"), _make_result(usage=usage_data)]
    fake_client = _make_fake_client(msgs)

    usage_mgr = _make_fake_usage_manager()
    # 给 get_thread_summary 一个返回值（complete 路径会 await 它做 broadcast）
    fake_summary = MagicMock()
    fake_summary.model_dump = MagicMock(return_value={"channel": "anthropic"})
    usage_mgr.get_thread_summary = AsyncMock(return_value=fake_summary)
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

    # **关键断言**：record_run_usage 不应该被调用
    usage_mgr.record_run_usage.assert_not_awaited()
    # 但 get_thread_summary 仍应被调（complete 触发 broadcast 用）
    usage_mgr.get_thread_summary.assert_awaited()


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
async def test_multiple_queries_each_broadcast_usage_summary() -> None:
    """多次 query 每次 complete 都触发一次 broadcast（不再依赖 run_counter）。"""
    usage_data = {"input_tokens": 10, "output_tokens": 5}
    usage_mgr = _make_fake_usage_manager()
    fake_summary = MagicMock()
    fake_summary.model_dump = MagicMock(return_value={"channel": "anthropic"})
    usage_mgr.get_thread_summary = AsyncMock(return_value=fake_summary)
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

    # complete 触发 get_thread_summary 2 次
    assert usage_mgr.get_thread_summary.await_count == 2
    # record_run_usage 全程不调
    usage_mgr.record_run_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_usage_summary_broadcast_on_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """**v0.1**：complete 路径 emit usage_summary_updated broadcast（zero write，纯派生）。"""
    usage_data = {"input_tokens": 100, "output_tokens": 50}
    msgs = [_make_assistant("hello"), _make_result(usage=usage_data)]
    fake_client = _make_fake_client(msgs)

    usage_mgr = _make_fake_usage_manager()
    # get_thread_summary 返回一个 mock summary
    fake_summary = MagicMock()
    fake_summary.model_dump = MagicMock(
        return_value={"channel": "anthropic", "cumulative_input_tokens": 100}
    )
    usage_mgr.get_thread_summary = AsyncMock(return_value=fake_summary)
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    # mock broadcaster
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()
    mock_broadcaster.emit = AsyncMock()
    monkeypatch.setattr("web.claude_code.service.get_broadcaster", lambda: mock_broadcaster)

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

    # 断言 broadcaster.broadcast 被调用，且参数包含 usage_summary_updated
    broadcast_calls = [
        c
        for c in mock_broadcaster.broadcast.call_args_list
        if isinstance(c[0][0], dict) and c[0][0].get("type") == "usage_summary_updated"
    ]
    assert len(broadcast_calls) == 1
    payload = broadcast_calls[0][0][0]
    assert payload["threadId"] == "thread-aabbccddeeff"
    assert payload["usage_summary"]["channel"] == "anthropic"


@pytest.mark.asyncio
async def test_assistant_message_model_passed_to_set_last_assistant_usage() -> None:
    """AssistantMessage.model 传入 set_last_assistant_usage 的 model kwarg。

    （原 test_record_run_usage_passes_model_from_assistant_message 改造：
    record_run_usage 不再被调，model 现在只通过 set_last_assistant_usage 注入内存）
    """
    assistant_usage = {"input_tokens": 100, "output_tokens": 50}
    assistant = AssistantMessage(
        content=[TextBlock(text="hello")],
        model="claude-opus-4",
        parent_tool_use_id=None,
        message_id="msg-1",
        usage=assistant_usage,
    )
    msgs = [assistant, _make_result(usage={"input_tokens": 100, "output_tokens": 50})]
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
    await svc.query(
        "hi",
        {"sessionId": "sid-1"},
        writer,
        register_id_override="thread-aabbccddeeff",
    )

    usage_mgr.set_last_assistant_usage.assert_called_once()
    call_kwargs = usage_mgr.set_last_assistant_usage.call_args
    assert call_kwargs[1]["model"] == "claude-opus-4"
    # record_run_usage 全程不调
    usage_mgr.record_run_usage.assert_not_awaited()


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


# ---------------------------------------------------------------------------
# set_last_assistant_usage 测试用例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_last_assistant_usage_called_on_assistant_message() -> None:
    """AssistantMessage with usage + model → set_last_assistant_usage 被调用。"""
    assistant_usage = {
        "input_tokens": 50000,
        "output_tokens": 2000,
        "cache_read_input_tokens": 400000,
        "cache_creation_input_tokens": 10000,
    }
    msgs = [
        _make_assistant("hello", model="claude-opus-4", usage=assistant_usage),
        _make_result(usage={"input_tokens": 60000, "output_tokens": 3000}),
    ]
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
    await svc.query(
        "hi",
        {"sessionId": "sid-1"},
        writer,
        register_id_override="thread-aabbccddeeff",
    )

    # 断言 set_last_assistant_usage 被调用
    usage_mgr.set_last_assistant_usage.assert_called_once()
    call_kwargs = usage_mgr.set_last_assistant_usage.call_args
    assert call_kwargs[0][0] == "thread-aabbccddeeff"
    assert call_kwargs[1]["channel"] == "anthropic"
    assert call_kwargs[1]["raw_payload"] == assistant_usage
    assert call_kwargs[1]["model"] == "claude-opus-4"

    # **v0.1 防回归**：record_run_usage 不应该被调
    usage_mgr.record_run_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_last_assistant_usage_skipped_when_no_usage() -> None:
    """AssistantMessage.usage = None → 不调 set_last_assistant_usage。"""
    msgs = [
        _make_assistant("hello", usage=None),
        _make_result(),
    ]
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
    await svc.query(
        "hi",
        {"sessionId": "sid-1"},
        writer,
        register_id_override="thread-aabbccddeeff",
    )

    # usage=None → 跳过
    usage_mgr.set_last_assistant_usage.assert_not_called()


@pytest.mark.asyncio
async def test_set_last_assistant_usage_called_per_assistant_message() -> None:
    """同一 query 多个 AssistantMessage → 每次都调（不去重）。"""
    usage1 = {"input_tokens": 10000, "output_tokens": 500}
    usage2 = {"input_tokens": 20000, "output_tokens": 1000}
    msgs = [
        _make_assistant("part1", usage=usage1),
        _make_assistant("part2", usage=usage2),
        _make_result(),
    ]
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
    await svc.query(
        "hi",
        {"sessionId": "sid-1"},
        writer,
        register_id_override="thread-aabbccddeeff",
    )

    # 每个 AssistantMessage 都调一次
    assert usage_mgr.set_last_assistant_usage.call_count == 2
    first_call = usage_mgr.set_last_assistant_usage.call_args_list[0]
    second_call = usage_mgr.set_last_assistant_usage.call_args_list[1]
    assert first_call[1]["raw_payload"] == usage1
    assert second_call[1]["raw_payload"] == usage2
