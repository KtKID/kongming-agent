"""SDK Bridge 单元测试。

全部 mock ``ClaudeSDKClient`` —— **不发真实 SDK 请求**，覆盖：

- 首次 / 二次 ``handle_start`` 路径
- ``ClaudeAgentOptions`` 字段透传（含 ``include_partial_messages=True`` 硬开）
- ``contextPackPath`` → ``SystemPromptFile``
- ``continuePointSnapshot`` 处理（方案 D：跟 contextPackPath 组合三种情况）
- ``set_can_use_tool`` 注入
- ``shutdown`` 清理
- ``interrupt`` / ``reconnect`` 抛 ``NotImplementedError``
- SDK 启动失败兜底
- pump 任务消费 message 流
- main.py 集成（mock 注入工厂）
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import ClaudeAgentOptions, CLIConnectionError, CLINotFoundError

from claude_sidecar.main import run_sidecar
from claude_sidecar.output import OutputWriter
from claude_sidecar.protocol import ReconnectRequest, StartRequest
from claude_sidecar.runtime import RunContext
from claude_sidecar.sdk_bridge import ClaudeSdkBridge

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_start_request(**overrides: Any) -> StartRequest:
    """构造 ``StartRequest``，可用 kwargs 覆盖默认字段。"""
    payload: dict[str, Any] = {
        "protocolVersion": "1",
        "type": "start",
        "workspaceId": "ws_1",
        "threadId": "th_1",
        "runId": "run_1",
        "cwd": "/tmp",
        "prompt": "hi",
    }
    payload.update({k: v for k, v in overrides.items() if v is not None})
    return StartRequest.model_validate(payload)


def _make_mock_client(
    *,
    receive_messages: list[Any] | None = None,
    connect_raises: BaseException | None = None,
) -> MagicMock:
    """生成 mock ``ClaudeSDKClient``。

    - ``connect()`` / ``query()`` / ``disconnect()`` 是 AsyncMock
    - ``receive_messages()`` 返回 async iterator，可控制吐出的 message
    """
    client = MagicMock()
    client.connect = AsyncMock(side_effect=connect_raises) if connect_raises else AsyncMock()
    client.query = AsyncMock()
    client.disconnect = AsyncMock()

    messages = receive_messages or []

    async def _aiter() -> AsyncIterator[Any]:
        for m in messages:
            yield m
        # 流结束后阻塞等取消（模拟真 SDK 的长连接）
        await asyncio.Event().wait()

    client.receive_messages = MagicMock(side_effect=lambda: _aiter())
    return client


def _make_translator() -> MagicMock:
    """生成 mock ``MessageTranslator``。"""
    t = MagicMock()
    t.handle = AsyncMock()
    t.add_pending_deny = MagicMock()
    t.reset_for_new_run = MagicMock()
    return t


def _make_writer() -> OutputWriter:
    """生成写到 ``io.StringIO`` 的 ``OutputWriter``。"""
    return OutputWriter(io.StringIO())


def _output_lines(writer: OutputWriter) -> list[dict[str, Any]]:
    """把 ``OutputWriter`` 内部 stream 收集的 JSONL 解析回 list."""
    stream = writer._stream  # type: ignore[attr-defined]
    raw = stream.getvalue()
    return [json.loads(line) for line in raw.strip().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. 首次 handle_start
# ---------------------------------------------------------------------------


async def test_first_start_creates_client_with_partial_messages() -> None:
    """首次 start：调 client_factory(options) 一次；include_partial_messages=True 必开。"""
    client = _make_mock_client()
    captured_options: list[ClaudeAgentOptions] = []

    def factory(opts: ClaudeAgentOptions) -> Any:
        captured_options.append(opts)
        return client

    bridge = ClaudeSdkBridge(_make_translator(), _make_writer(), client_factory=factory)
    await bridge.handle_start(_make_start_request())

    assert len(captured_options) == 1, "首次 start 应建一次 client"
    assert captured_options[0].include_partial_messages is True
    await bridge.shutdown()


async def test_first_start_calls_connect_then_query() -> None:
    """调用顺序：connect() → query(prompt=...)。"""
    client = _make_mock_client()
    bridge = ClaudeSdkBridge(
        _make_translator(),
        _make_writer(),
        client_factory=lambda _: client,
    )
    await bridge.handle_start(_make_start_request(prompt="hello"))

    assert client.connect.await_count == 1
    assert client.query.await_count == 1
    assert client.query.await_args.kwargs.get("prompt") == "hello"
    await bridge.shutdown()


# ---------------------------------------------------------------------------
# 2. 二次 handle_start
# ---------------------------------------------------------------------------


async def test_second_start_reuses_client() -> None:
    """二次 start：不再调 client_factory；调 query；translator.reset_for_new_run 触发。"""
    client = _make_mock_client()
    factory_calls = 0

    def factory(_: ClaudeAgentOptions) -> Any:
        nonlocal factory_calls
        factory_calls += 1
        return client

    translator = _make_translator()
    bridge = ClaudeSdkBridge(translator, _make_writer(), client_factory=factory)

    # 第一次
    await bridge.handle_start(_make_start_request(runId="run_1", prompt="first"))
    # 第二次
    await bridge.handle_start(_make_start_request(runId="run_2", prompt="second"))

    assert factory_calls == 1, "二次 start 不应再建 client"
    assert client.connect.await_count == 1, "二次 start 不应再 connect"
    assert client.query.await_count == 2
    # 第二次时 reset_for_new_run 必须被调
    assert translator.reset_for_new_run.call_count == 1
    ctx_arg = translator.reset_for_new_run.call_args.args[0]
    assert isinstance(ctx_arg, RunContext)
    assert ctx_arg.run_id == "run_2"
    await bridge.shutdown()


# ---------------------------------------------------------------------------
# 3. options 透传
# ---------------------------------------------------------------------------


async def test_options_passthrough_all_fields() -> None:
    """model / cwd / permission_mode / allowed_tools / disallowed_tools / resume 都映射进 options。"""
    client = _make_mock_client()
    captured: list[ClaudeAgentOptions] = []

    def factory(opts: ClaudeAgentOptions) -> Any:
        captured.append(opts)
        return client

    bridge = ClaudeSdkBridge(_make_translator(), _make_writer(), client_factory=factory)
    request = _make_start_request(
        model="claude-sonnet-4-5",
        cwd="/tmp/work",
        permissionMode="acceptEdits",
        allowedTools=["Read", "Bash"],
        disallowedTools=["Write"],
        resume="sess-abc",
    )
    await bridge.handle_start(request)

    opts = captured[0]
    assert opts.model == "claude-sonnet-4-5"
    assert opts.cwd == "/tmp/work"
    assert opts.permission_mode == "acceptEdits"
    assert opts.allowed_tools == ["Read", "Bash"]
    assert opts.disallowed_tools == ["Write"]
    assert opts.resume == "sess-abc"
    await bridge.shutdown()


# ---------------------------------------------------------------------------
# 4. system_prompt 三种组合（方案 D）
# ---------------------------------------------------------------------------


async def test_context_pack_path_becomes_system_prompt_file() -> None:
    """contextPackPath → SystemPromptFile{type:'file', path:...}，sidecar 不读文件。"""
    client = _make_mock_client()
    captured: list[ClaudeAgentOptions] = []
    bridge = ClaudeSdkBridge(
        _make_translator(),
        _make_writer(),
        client_factory=lambda o: captured.append(o) or client,
    )
    await bridge.handle_start(_make_start_request(contextPackPath="/tmp/ctx.md"))
    opts = captured[0]
    assert opts.system_prompt == {"type": "file", "path": "/tmp/ctx.md"}
    await bridge.shutdown()


async def test_continue_point_snapshot_with_context_pack_prefixes_prompt() -> None:
    """同时有 contextPackPath + continuePointSnapshot：snapshot 拼到 prompt 前缀，
    system_prompt 仍是 SystemPromptFile。"""
    client = _make_mock_client()
    captured: list[ClaudeAgentOptions] = []
    bridge = ClaudeSdkBridge(
        _make_translator(),
        _make_writer(),
        client_factory=lambda o: captured.append(o) or client,
    )
    await bridge.handle_start(
        _make_start_request(
            contextPackPath="/tmp/ctx.md",
            continuePointSnapshot="resume from step 3",
            prompt="user query",
        )
    )
    opts = captured[0]
    assert opts.system_prompt == {"type": "file", "path": "/tmp/ctx.md"}
    sent_prompt = client.query.await_args.kwargs.get("prompt")
    assert "resume from step 3" in sent_prompt
    assert "user query" in sent_prompt
    # 顺序：snapshot 在 user query 之前
    assert sent_prompt.index("resume from step 3") < sent_prompt.index("user query")
    await bridge.shutdown()


async def test_continue_point_snapshot_without_context_pack_uses_preset_append() -> None:
    """只有 continuePointSnapshot：用 SystemPromptPreset(append=...)。"""
    client = _make_mock_client()
    captured: list[ClaudeAgentOptions] = []
    bridge = ClaudeSdkBridge(
        _make_translator(),
        _make_writer(),
        client_factory=lambda o: captured.append(o) or client,
    )
    await bridge.handle_start(
        _make_start_request(
            continuePointSnapshot="continue here",
            prompt="user query",
        )
    )
    opts = captured[0]
    assert opts.system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "append": "continue here",
    }
    sent_prompt = client.query.await_args.kwargs.get("prompt")
    # 此时 prompt 应该是原始 user query，没有 snapshot 前缀
    assert sent_prompt == "user query"
    await bridge.shutdown()


async def test_no_system_prompt_when_neither_field_set() -> None:
    """contextPackPath + continuePointSnapshot 都没有：system_prompt 为 None。"""
    client = _make_mock_client()
    captured: list[ClaudeAgentOptions] = []
    bridge = ClaudeSdkBridge(
        _make_translator(),
        _make_writer(),
        client_factory=lambda o: captured.append(o) or client,
    )
    await bridge.handle_start(_make_start_request())
    opts = captured[0]
    assert opts.system_prompt is None
    await bridge.shutdown()


# ---------------------------------------------------------------------------
# 5. set_can_use_tool
# ---------------------------------------------------------------------------


async def test_set_can_use_tool_injects_callback() -> None:
    """set_can_use_tool 在 start 之前调用：options.can_use_tool 被注入。"""
    captured: list[ClaudeAgentOptions] = []
    client = _make_mock_client()
    bridge = ClaudeSdkBridge(
        _make_translator(),
        _make_writer(),
        client_factory=lambda o: captured.append(o) or client,
    )

    async def cb(*args: Any, **kwargs: Any) -> Any:
        return None

    bridge.set_can_use_tool(cb)
    await bridge.handle_start(_make_start_request())
    assert captured[0].can_use_tool is cb
    await bridge.shutdown()


# ---------------------------------------------------------------------------
# 6. shutdown
# ---------------------------------------------------------------------------


async def test_shutdown_calls_disconnect_and_cancels_pump() -> None:
    """shutdown：cancel pump task + 调 client.disconnect()。"""
    client = _make_mock_client()
    bridge = ClaudeSdkBridge(
        _make_translator(),
        _make_writer(),
        client_factory=lambda _: client,
    )
    await bridge.handle_start(_make_start_request())
    # pump task 起来了
    assert bridge._message_pump_task is not None
    assert not bridge._message_pump_task.done()

    await bridge.shutdown()
    assert client.disconnect.await_count == 1
    # pump 已 cancel + cleared
    assert bridge._message_pump_task is None
    assert bridge._client is None


async def test_shutdown_idempotent() -> None:
    """shutdown 调用两次不报错（幂等）。"""
    client = _make_mock_client()
    bridge = ClaudeSdkBridge(
        _make_translator(),
        _make_writer(),
        client_factory=lambda _: client,
    )
    await bridge.handle_start(_make_start_request())
    await bridge.shutdown()
    await bridge.shutdown()  # 第二次不应抛错
    assert client.disconnect.await_count == 1


async def test_shutdown_without_start_is_noop() -> None:
    """从未 start 时 shutdown 也不报错。"""
    bridge = ClaudeSdkBridge(_make_translator(), _make_writer())
    await bridge.shutdown()  # client / pump 都是 None，不应抛


# ---------------------------------------------------------------------------
# 7. interrupt / reconnect: NotImplementedError
# ---------------------------------------------------------------------------


async def test_interrupt_raises_not_implemented() -> None:
    bridge = ClaudeSdkBridge(_make_translator(), _make_writer())
    with pytest.raises(NotImplementedError):
        await bridge.interrupt()


async def test_reconnect_raises_not_implemented() -> None:
    bridge = ClaudeSdkBridge(_make_translator(), _make_writer())
    request = ReconnectRequest.model_validate(
        {
            "protocolVersion": "1",
            "type": "reconnect",
            "workspaceId": "ws_1",
            "threadId": "th_1",
            "runId": "run_1",
            "runtimeSessionId": "sess-x",
        }
    )
    with pytest.raises(NotImplementedError):
        await bridge.reconnect(request)


# ---------------------------------------------------------------------------
# 8. SDK 启动失败兜底
# ---------------------------------------------------------------------------


async def test_sdk_start_failure_emits_transport_error_no_run_id() -> None:
    """首次 start 时 connect() 抛 CLINotFoundError → emit transport_error，不带 runId。"""
    client = _make_mock_client(connect_raises=CLINotFoundError("CLI not found"))
    writer = _make_writer()
    bridge = ClaudeSdkBridge(
        _make_translator(),
        writer,
        client_factory=lambda _: client,
    )
    await bridge.handle_start(_make_start_request())

    events = _output_lines(writer)
    assert len(events) == 1
    err = events[0]
    assert err["type"] == "claude_transport_error"
    assert err["recoverable"] is False
    assert err["threadId"] == "th_1"
    assert "runId" not in err  # 首次 SDK 起不来 → run 未建立 → 不带 runId
    # client 已被重置，下次 start 会重试
    assert bridge._client is None


async def test_sdk_connection_error_emits_transport_error() -> None:
    """connect() 抛 CLIConnectionError 也走相同兜底路径。"""
    client = _make_mock_client(connect_raises=CLIConnectionError("connection failed"))
    writer = _make_writer()
    bridge = ClaudeSdkBridge(
        _make_translator(),
        writer,
        client_factory=lambda _: client,
    )
    await bridge.handle_start(_make_start_request())
    events = _output_lines(writer)
    assert events[0]["type"] == "claude_transport_error"
    assert events[0]["recoverable"] is False


# ---------------------------------------------------------------------------
# 9. message pump 把 SDK message 喂给 translator
# ---------------------------------------------------------------------------


async def test_message_pump_calls_translator_handle() -> None:
    """SDK receive_messages 吐 N 条 message → translator.handle 被调 N 次，带 RunContext。"""
    msg1 = MagicMock(name="msg1")
    msg2 = MagicMock(name="msg2")
    client = _make_mock_client(receive_messages=[msg1, msg2])
    translator = _make_translator()
    bridge = ClaudeSdkBridge(translator, _make_writer(), client_factory=lambda _: client)

    await bridge.handle_start(_make_start_request())

    # 等 pump 消费 2 条 message（不能 join 整个 task，因为 mock 流后还会阻塞等取消）
    for _ in range(50):  # 最多 ~0.5s 等待
        if translator.handle.await_count >= 2:
            break
        await asyncio.sleep(0.01)

    assert translator.handle.await_count == 2
    # 每次都传 (sdk_message, run_context)
    for call in translator.handle.await_args_list:
        _msg_arg, ctx_arg = call.args
        assert isinstance(ctx_arg, RunContext)
        assert ctx_arg.thread_id == "th_1"
        assert ctx_arg.run_id == "run_1"

    await bridge.shutdown()


# ---------------------------------------------------------------------------
# 10. main.py 集成（mock 工厂注入）
# ---------------------------------------------------------------------------


async def test_main_routes_start_to_sdk_bridge() -> None:
    """run_sidecar 通过工厂注入 mock bridge：start 请求路由到 bridge.handle_start。"""
    fake_bridge = MagicMock()
    fake_bridge.handle_start = AsyncMock()
    fake_bridge.shutdown = AsyncMock()

    fake_translator = _make_translator()

    start_payload = {
        "protocolVersion": "1",
        "type": "start",
        "workspaceId": "ws_1",
        "threadId": "th_1",
        "runId": "run_1",
        "cwd": "/tmp",
        "prompt": "hi",
    }
    shutdown_payload = {"protocolVersion": "1", "type": "shutdown", "threadId": "th_1"}

    stdin = io.StringIO(json.dumps(start_payload) + "\n" + json.dumps(shutdown_payload) + "\n")
    stdout = io.StringIO()

    code = await run_sidecar(
        stdin=stdin,
        stdout=stdout,
        sdk_bridge_factory=lambda _t, _w: fake_bridge,
        translator_factory=lambda _w: fake_translator,
    )
    assert code == 0
    assert fake_bridge.handle_start.await_count == 1
    sent_request = fake_bridge.handle_start.await_args.args[0]
    assert isinstance(sent_request, StartRequest)
    assert sent_request.run_id == "run_1"
    # shutdown 也调了 bridge.shutdown
    assert fake_bridge.shutdown.await_count >= 1


async def test_main_falls_back_when_translator_unavailable() -> None:
    """translator_factory 抛 ImportError → bridge 装配失败 → start 走 not_implemented fallback。"""

    def failing_factory(_w: OutputWriter) -> Any:
        raise ImportError("simulated: translator.py not yet merged")

    start_payload = {
        "protocolVersion": "1",
        "type": "start",
        "workspaceId": "ws_1",
        "threadId": "th_1",
        "runId": "run_1",
        "cwd": "/tmp",
        "prompt": "hi",
    }
    shutdown_payload = {"protocolVersion": "1", "type": "shutdown", "threadId": "th_1"}

    stdin = io.StringIO(json.dumps(start_payload) + "\n" + json.dumps(shutdown_payload) + "\n")
    stdout = io.StringIO()

    code = await run_sidecar(
        stdin=stdin,
        stdout=stdout,
        translator_factory=failing_factory,
    )
    assert code == 0
    lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    # ready / start fallback / shutdown ack
    assert lines[0]["type"] == "claude_sidecar_ready"
    fallback = lines[1]
    assert fallback["type"] == "claude_transport_error"
    assert fallback["recoverable"] is True
    assert "not implemented" in fallback["message"].lower()
    assert fallback["threadId"] == "th_1"
    assert fallback["runId"] == "run_1"


async def test_main_resolve_approval_still_not_implemented() -> None:
    """resolve_approval 仍未实现（等 #4），main 应回 not_implemented，不带 runId。"""
    fake_bridge = MagicMock()
    fake_bridge.handle_start = AsyncMock()
    fake_bridge.shutdown = AsyncMock()
    fake_translator = _make_translator()

    resolve_payload = {
        "protocolVersion": "1",
        "type": "resolve_approval",
        "threadId": "th_1",
        "requestId": "req_1",
        "approved": True,
    }
    shutdown_payload = {"protocolVersion": "1", "type": "shutdown", "threadId": "th_1"}

    stdin = io.StringIO(json.dumps(resolve_payload) + "\n" + json.dumps(shutdown_payload) + "\n")
    stdout = io.StringIO()
    await run_sidecar(
        stdin=stdin,
        stdout=stdout,
        sdk_bridge_factory=lambda _t, _w: fake_bridge,
        translator_factory=lambda _w: fake_translator,
    )
    lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    not_impl = lines[1]
    assert not_impl["type"] == "claude_transport_error"
    assert not_impl["recoverable"] is True
    assert "not implemented" in not_impl["message"].lower()
    assert not_impl["threadId"] == "th_1"
    assert "runId" not in not_impl  # resolve_approval 不带 runId


async def test_main_interrupt_still_not_implemented() -> None:
    """interrupt 仍未实现（等 #5），main 不应路由到 bridge.interrupt。"""
    fake_bridge = MagicMock()
    fake_bridge.handle_start = AsyncMock()
    fake_bridge.interrupt = AsyncMock()
    fake_bridge.shutdown = AsyncMock()
    fake_translator = _make_translator()

    interrupt_payload = {
        "protocolVersion": "1",
        "type": "interrupt",
        "threadId": "th_1",
    }
    shutdown_payload = {"protocolVersion": "1", "type": "shutdown", "threadId": "th_1"}

    stdin = io.StringIO(json.dumps(interrupt_payload) + "\n" + json.dumps(shutdown_payload) + "\n")
    stdout = io.StringIO()
    await run_sidecar(
        stdin=stdin,
        stdout=stdout,
        sdk_bridge_factory=lambda _t, _w: fake_bridge,
        translator_factory=lambda _w: fake_translator,
    )
    # bridge.interrupt 不应被调（main 还没接通这条命令）
    assert fake_bridge.interrupt.await_count == 0
    lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    not_impl = lines[1]
    assert "not implemented" in not_impl["message"].lower()
