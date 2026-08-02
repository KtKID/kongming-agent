"""ClaudeCodeService 单测：mock ClaudeSDKClient，验证 options 装配 + 多 run 复用 + abort。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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


class _ReconnectableWriter:
    def __init__(self, sink: _FakeWriter) -> None:
        self._sink = sink

    def attach_ws(self, sink: _FakeWriter) -> None:
        self._sink = sink

    async def send_json(self, msg: dict[str, Any]) -> None:
        await self._sink.send_json(msg)


def _make_fake_client(messages: list[Any]) -> MagicMock:
    """构造一个看起来像 ClaudeSDKClient 的 mock。"""
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


def _make_assistant(text: str, message_id: str = "msg-1") -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude",
        parent_tool_use_id=None,
        message_id=message_id,
    )


def _make_result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        session_id="sid",
        stop_reason="end_turn",
        total_cost_usd=0.0,
        usage={"input_tokens": 5, "output_tokens": 3},
        result="ok",
    )


# ---------------------------------------------------------------------------
# _build_options
# ---------------------------------------------------------------------------


def _make_service(client_factory: Any = None) -> ClaudeCodeService:
    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    return ClaudeCodeService(
        normalizer,
        approval,
        sessions,
        client_factory=client_factory or (lambda **_: _make_fake_client([])),
    )


def test_build_options_forces_include_partial_messages() -> None:
    svc = _make_service()
    opts = svc._build_options({})
    assert opts.include_partial_messages is True


def test_build_options_includes_can_use_tool() -> None:
    svc = _make_service()
    opts = svc._build_options({})
    # 比较 __func__：Python bound method 每次访问构造新对象
    assert opts.can_use_tool is not None
    assert opts.can_use_tool.__func__ is svc._approval.can_use_tool.__func__  # type: ignore[union-attr]


def test_build_options_passes_through_model_cwd() -> None:
    svc = _make_service()
    opts = svc._build_options(
        {
            "model": "sonnet",
            "cwd": "/tmp/x",
            "permissionMode": "default",
            "allowedTools": ["Read", "Write"],
            "disallowedTools": ["Bash"],
            "resume": "sess-old",
        },
    )
    assert opts.model == "sonnet"
    assert opts.cwd == "/tmp/x"
    assert opts.permission_mode == "default"
    assert opts.allowed_tools == ["Read", "Write"]
    assert opts.disallowed_tools == ["Bash"]
    assert opts.resume == "sess-old"


def test_build_options_context_pack_path_to_system_prompt_file() -> None:
    svc = _make_service()
    opts = svc._build_options({"contextPackPath": "/tmp/pack.md"})
    assert opts.system_prompt == {"type": "file", "path": "/tmp/pack.md"}


def test_build_options_snake_case_aliases() -> None:
    svc = _make_service()
    opts = svc._build_options(
        {
            "permission_mode": "plan",
            "allowed_tools": ["Read"],
            "disallowed_tools": ["Bash"],
            "context_pack_path": "/x.md",
        },
    )
    assert opts.permission_mode == "plan"
    assert opts.allowed_tools == ["Read"]
    assert opts.disallowed_tools == ["Bash"]
    assert opts.system_prompt == {"type": "file", "path": "/x.md"}


# ---------------------------------------------------------------------------
# query 主流程
# ---------------------------------------------------------------------------


async def test_query_sends_normalized_messages() -> None:
    """query 主循环：ClaudeSDKClient 输出 messages → normalize → writer.send_json。"""
    msgs = [_make_assistant("hello"), _make_result()]
    fake_client = _make_fake_client(msgs)

    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(
        normalizer,
        approval,
        sessions,
        client_factory=lambda **_: fake_client,
    )

    writer = _FakeWriter()
    await svc.query("hi", {"sessionId": "sid-1"}, writer)

    # 收到 text + complete
    kinds = [m.get("frame_type") for m in writer.sent]
    assert "text" in kinds
    assert "complete" in kinds


async def test_query_reconnect_rebinds_stream_writer() -> None:
    gate = asyncio.Event()

    async def receive_response() -> Any:
        yield _make_assistant("first", message_id="msg-1")
        await gate.wait()
        yield _make_assistant("second", message_id="msg-2")
        yield _make_result()

    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.disconnect = AsyncMock()
    fake_client.query = AsyncMock()
    fake_client.receive_response = receive_response
    fake_client.interrupt = AsyncMock()

    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(
        normalizer,
        approval,
        sessions,
        client_factory=lambda **_: fake_client,
    )

    first_sink = _FakeWriter()
    second_sink = _FakeWriter()
    writer = _ReconnectableWriter(first_sink)
    task = asyncio.create_task(svc.query("hi", {"sessionId": "sid-1"}, writer))

    for _ in range(50):
        if first_sink.sent:
            break
        await asyncio.sleep(0)
    assert first_sink.sent, "expected first frame before reconnect"

    replaced = await sessions.replace_writer("sid-1", second_sink)
    assert replaced is True
    gate.set()
    await task

    # protocol-frame-type-unify：原 `kind` 已统一改为 `frame_type` 作判别字段。
    assert [msg.get("frame_type") for msg in first_sink.sent] == ["text"]
    assert [msg.get("frame_type") for msg in second_sink.sent] == ["text", "complete"]


async def test_query_reuses_client_across_runs() -> None:
    """多 run 复用 ClaudeSDKClient。"""
    factory_calls = 0

    def factory(**_: Any) -> MagicMock:
        nonlocal factory_calls
        factory_calls += 1
        return _make_fake_client([_make_result()])

    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(normalizer, approval, sessions, client_factory=factory)

    writer = _FakeWriter()
    await svc.query("first", {"sessionId": "sid-reuse"}, writer)
    await svc.query("second", {"sessionId": "sid-reuse"}, writer)

    assert factory_calls == 1, "client should be reused for same sessionId"


async def test_query_creates_new_client_per_session() -> None:
    """不同 sessionId 用各自 client。"""
    factory_calls = 0

    def factory(**_: Any) -> MagicMock:
        nonlocal factory_calls
        factory_calls += 1
        return _make_fake_client([_make_result()])

    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(normalizer, approval, sessions, client_factory=factory)

    writer = _FakeWriter()
    await svc.query("a", {"sessionId": "sid-a"}, writer)
    await svc.query("b", {"sessionId": "sid-b"}, writer)
    assert factory_calls == 2


async def test_query_writer_session_isolated_to_approval() -> None:
    """query 期间 active_writer 设置；退出后清空。"""
    fake_client = _make_fake_client([_make_result()])
    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(normalizer, approval, sessions, client_factory=lambda **_: fake_client)

    writer = _FakeWriter()
    await svc.query("hi", {"sessionId": "sid-x"}, writer)
    # 完成后 active_writer 应为 None
    assert approval._active_writer is None


async def test_query_failure_emits_error_and_evicts_client() -> None:
    """SDK 抛错时 emit error 帧并清掉缓存的 client。"""
    bad_client = MagicMock()
    bad_client.connect = AsyncMock(side_effect=RuntimeError("boom"))
    bad_client.disconnect = AsyncMock()

    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(
        normalizer,
        approval,
        sessions,
        client_factory=lambda **_: bad_client,
    )

    writer = _FakeWriter()
    await svc.query("hi", {"sessionId": "sid-err"}, writer)
    # error 帧应当被发出
    kinds = [m.get("frame_type") for m in writer.sent]
    assert "error" in kinds


async def test_abort_calls_session_manager() -> None:
    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(normalizer, approval, sessions)

    # 先注册一个 session
    record = await sessions.register("sid-abort", _FakeWriter())
    record.query_task = asyncio.create_task(asyncio.sleep(10))

    ok = await svc.abort("sid-abort")
    assert ok is True
    assert record.abort_event.is_set() is True


async def test_abort_unknown_session_returns_false() -> None:
    svc = _make_service()
    assert await svc.abort("nope") is False


async def test_shutdown_all_disconnects_all_clients() -> None:
    fake_clients: list[MagicMock] = []

    def factory(**_: Any) -> MagicMock:
        client = _make_fake_client([_make_result()])
        fake_clients.append(client)
        return client

    sessions = SessionManager()
    normalizer = ClaudeNormalizer()
    approval = ApprovalBridge(normalizer, sessions)
    svc = ClaudeCodeService(normalizer, approval, sessions, client_factory=factory)

    writer = _FakeWriter()
    await svc.query("a", {"sessionId": "sid-1"}, writer)
    await svc.query("b", {"sessionId": "sid-2"}, writer)
    assert len(fake_clients) == 2

    await svc.shutdown_all()
    for c in fake_clients:
        c.disconnect.assert_awaited()
    assert svc._clients == {}
