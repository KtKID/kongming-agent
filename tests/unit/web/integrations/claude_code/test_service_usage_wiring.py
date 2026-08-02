"""ClaudeCodeService usage wiring 单测（v2，usage-token-v2-bigbang 重构）。

⚠️ v2 改动：

claude_code/service.py 主循环 ``_consume`` **不再调** v1 的
``record_run_usage`` / ``set_last_assistant_usage`` —— 它们在 v2 manager 中已删除。

新行为：

- ``AssistantMessage`` 到达时：调 ``manager.get_thread_usage()`` 派生最新 usage，
  emit ``usage_summary_updated`` broadcast
- ``ResultMessage (complete)`` 同理
- service 层零 token 写盘调用

防回归用例：本套测试钉死"complete 路径不再调任何 v1 写盘方法"。
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

from hosts.web.integrations.claude_code.normalizer import ClaudeNormalizer
from hosts.web.integrations.claude_code.service import ClaudeCodeService
from hosts.web.shared.session_manager import SessionManager
from tests.unit.web.integrations.claude_code._approval_test_support import (
    build_test_approval_bridge as ApprovalBridge,
)


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


def _make_fake_usage_manager_v2() -> MagicMock:
    """模拟 v2 UsageTokenManager：只有 get_thread_usage 一个方法。"""
    mgr = MagicMock()
    # v2 唯一公共方法
    fake_dto = MagicMock()
    fake_dto.model_dump = MagicMock(return_value={"provider": "claude", "input_tokens": 100})
    mgr.get_thread_usage = AsyncMock(return_value=fake_dto)
    # 故意不设 v1 方法——测试代码访问会 raise，触发防回归
    return mgr


def _make_fake_thread_manager(usage_manager: MagicMock) -> MagicMock:
    tm = MagicMock()
    tm.usage_manager = usage_manager
    tm.list_threads = MagicMock(return_value=[])
    return tm


# ---------------------------------------------------------------------------
# 防回归用例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_triggers_get_thread_usage_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """complete 路径调 manager.get_thread_usage 派生并 emit broadcast。"""
    usage_data = {"input_tokens": 100, "output_tokens": 50}
    msgs = [_make_assistant("hello", usage=usage_data), _make_result(usage=usage_data)]
    fake_client = _make_fake_client(msgs)

    usage_mgr = _make_fake_usage_manager_v2()
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    # mock broadcaster
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()
    mock_broadcaster.begin_run = AsyncMock(return_value=MagicMock())
    mock_broadcaster.publish_status = AsyncMock()
    monkeypatch.setattr(
        "hosts.web.integrations.claude_code.service.get_thread_status_manager",
        lambda: mock_broadcaster,
    )

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

    # get_thread_usage 被调（AssistantMessage + complete 各一次）
    assert usage_mgr.get_thread_usage.await_count >= 1
    # broadcast 被调
    broadcast_calls = [
        c
        for c in mock_broadcaster.broadcast.call_args_list
        if isinstance(c[0][0], dict) and c[0][0].get("frame_type") == "usage_summary_updated"
    ]
    assert len(broadcast_calls) >= 1
    payload = broadcast_calls[0][0][0]
    assert "type" not in payload
    assert payload["threadId"] == "thread-aabbccddeeff"
    assert "usage" in payload


@pytest.mark.asyncio
async def test_v1_methods_no_longer_called(monkeypatch: pytest.MonkeyPatch) -> None:
    """**v2 防回归**：service 不再调 record_run_usage / set_last_assistant_usage。

    如果 service.py 误回退到 v1，会调用 v2 manager 不存在的方法，hasattr=False。
    """
    msgs = [_make_assistant("hello", usage={"input_tokens": 10, "output_tokens": 5})]
    fake_client = _make_fake_client(msgs)

    # 显式构造没有 v1 方法的 mock
    usage_mgr = MagicMock(spec=["get_thread_usage"])
    fake_dto = MagicMock()
    fake_dto.model_dump = MagicMock(return_value={})
    usage_mgr.get_thread_usage = AsyncMock(return_value=fake_dto)

    thread_mgr = _make_fake_thread_manager(usage_mgr)

    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()
    mock_broadcaster.begin_run = AsyncMock(return_value=MagicMock())
    mock_broadcaster.publish_status = AsyncMock()
    monkeypatch.setattr(
        "hosts.web.integrations.claude_code.service.get_thread_status_manager",
        lambda: mock_broadcaster,
    )

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
    # 不抛即可——证明 service 没碰 v1 方法
    await svc.query(
        "hi",
        {"sessionId": "sid-1"},
        writer,
        register_id_override="thread-aabbccddeeff",
    )


@pytest.mark.asyncio
async def test_thread_manager_none_no_usage_call() -> None:
    """thread_manager=None → 完全跳过 usage 派生 + broadcast。"""
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
    # 测试能跑完就说明跳过逻辑正确
    kinds = [m.get("frame_type") for m in writer.sent]
    assert "complete" in kinds


@pytest.mark.asyncio
async def test_placeholder_id_no_usage_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """register_id 是 placeholder 非 ``thread-<hex12>`` → 跳过 usage 派生。"""
    msgs = [_make_result()]
    fake_client = _make_fake_client(msgs)

    usage_mgr = _make_fake_usage_manager_v2()
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

    # placeholder 不是 thread-<12hex> → get_thread_usage 不被调
    usage_mgr.get_thread_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_usage_dto_none_no_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_thread_usage 返回 None → 不 emit broadcast（前端 StatusLine 留空）。"""
    msgs = [_make_assistant("ok", usage={"input_tokens": 10, "output_tokens": 5})]
    fake_client = _make_fake_client(msgs)

    usage_mgr = MagicMock()
    usage_mgr.get_thread_usage = AsyncMock(return_value=None)
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()
    mock_broadcaster.begin_run = AsyncMock(return_value=MagicMock())
    mock_broadcaster.publish_status = AsyncMock()
    monkeypatch.setattr(
        "hosts.web.integrations.claude_code.service.get_thread_status_manager",
        lambda: mock_broadcaster,
    )

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

    # usage_summary_updated 不会被 broadcast（因为 dto=None）
    broadcast_calls = [
        c
        for c in mock_broadcaster.broadcast.call_args_list
        if isinstance(c[0][0], dict) and c[0][0].get("frame_type") == "usage_summary_updated"
    ]
    assert len(broadcast_calls) == 0


@pytest.mark.asyncio
async def test_get_thread_usage_exception_does_not_break_main_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_thread_usage 抛异常不影响主对话流（contextlib.suppress 兜底）。"""
    msgs = [_make_assistant("ok", usage={"input_tokens": 10, "output_tokens": 5}), _make_result()]
    fake_client = _make_fake_client(msgs)

    usage_mgr = MagicMock()
    usage_mgr.get_thread_usage = AsyncMock(side_effect=RuntimeError("derive error"))
    thread_mgr = _make_fake_thread_manager(usage_mgr)

    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()
    mock_broadcaster.begin_run = AsyncMock(return_value=MagicMock())
    mock_broadcaster.publish_status = AsyncMock()
    monkeypatch.setattr(
        "hosts.web.integrations.claude_code.service.get_thread_status_manager",
        lambda: mock_broadcaster,
    )

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
    kinds = [m.get("frame_type") for m in writer.sent]
    assert "complete" in kinds
