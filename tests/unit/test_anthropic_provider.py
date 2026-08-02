"""unit：AnthropicMessagesProvider 消息格式转换 + 分发逻辑。

覆盖：
- _split_system_and_convert：system 提取、tool_result 合并、tool_use blocks
- _tools_to_anthropic_format：tools schema 无 function 包装
- _parse_response：stop_reason 映射、usage 归一化、tool_use block 解析
- _normalize_stop_reason：各种 stop_reason 映射
- session_engine 分发：provider=anthropic → AnthropicMessagesProvider
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.contracts import LLMRequest
from core.message import Message, ToolCall
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.model_provider_catalog import (
    ProviderProtocol,
    ReasoningAdapter,
    ReasoningCapability,
)
from infrastructure.config.models import ApiKeyHeader, Config, ModelSelectionConfig
from infrastructure.llm_providers.anthropic_messages import AnthropicMessagesProvider
from tests._helpers.model_runtime import make_model_runtime

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_provider(
    api_key: str = "sk-ant-test",
    base_url: str = "https://api.anthropic.com",
    name: str = "claude-sonnet-4-5",
    api_key_header: ApiKeyHeader | None = None,
) -> AnthropicMessagesProvider:
    runtime, credential = make_model_runtime(
        protocol=ProviderProtocol.ANTHROPIC,
        name=name,
        base_url=base_url,
        api_key=api_key,
        api_key_header=api_key_header,
    )
    return AnthropicMessagesProvider(
        model_config=runtime,
        credential=credential,
    )


def _minimax_reasoning_profile() -> ReasoningCapability:
    """MiniMax-M3 Anthropic 兼容端点的 catalog reasoning capability。"""
    return ReasoningCapability(
        adapter=ReasoningAdapter.CONFIGURABLE_PATCH,
        enabled_patch={"thinking": {"type": "adaptive"}},
        disabled_patch={"thinking": {"type": "disabled"}},
        supported_efforts=("high",),
        default_effort="high",
        supports_disabled=True,
        effort_map={
            "low": "high",
            "medium": "high",
            "high": "high",
            "max": "high",
        },
    )


# ---------------------------------------------------------------------------
# 消息格式转换
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_split_system_extracted_to_top_level() -> None:
    """system 消息应提取到 system_text，不出现在 messages 数组。"""
    messages = [
        Message.system("You are a helpful assistant."),
        Message.user("hello"),
    ]
    system_text, out = AnthropicMessagesProvider._split_system_and_convert(messages)
    assert system_text == "You are a helpful assistant."
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert "system" not in [m["role"] for m in out]


@pytest.mark.unit
def test_split_multiple_system_messages_joined() -> None:
    """多条 system 消息合并为单一 system_text。"""
    messages = [
        Message.system("Part one."),
        Message.system("Part two."),
        Message.user("hi"),
    ]
    system_text, _ = AnthropicMessagesProvider._split_system_and_convert(messages)
    assert system_text == "Part one.\n\nPart two."


@pytest.mark.unit
def test_split_assistant_with_tool_calls_converted_to_tool_use() -> None:
    """assistant 消息的 tool_calls → tool_use content blocks。"""
    messages = [
        Message.user("run it"),
        Message.assistant(
            content=None,
            tool_calls=[ToolCall(call_id="c1", tool_name="shell", arguments={"cmd": "ls"})],
        ),
    ]
    _, out = AnthropicMessagesProvider._split_system_and_convert(messages)
    asst = out[1]
    assert asst["role"] == "assistant"
    assert isinstance(asst["content"], list)
    block = asst["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "c1"
    assert block["name"] == "shell"
    assert block["input"] == {"cmd": "ls"}


@pytest.mark.unit
def test_split_tool_result_merged_into_user_list() -> None:
    """tool 角色消息应合并进前一条 user 消息的 content list。"""
    messages = [
        Message.user("run it"),
        Message.assistant(
            content=None,
            tool_calls=[ToolCall(call_id="c1", tool_name="shell", arguments={})],
        ),
        Message(role="tool", content="ok", tool_call_id="c1"),
    ]
    _, out = AnthropicMessagesProvider._split_system_and_convert(messages)
    # 应该是: user, assistant, user(tool_result)
    assert out[-1]["role"] == "user"
    content = out[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "tool_result"
    assert content[0]["tool_use_id"] == "c1"
    assert content[0]["content"] == "ok"


@pytest.mark.unit
def test_split_assistant_text_only() -> None:
    """纯文本 assistant 消息 → text block。"""
    messages = [Message.user("hi"), Message.assistant(content="hello")]
    _, out = AnthropicMessagesProvider._split_system_and_convert(messages)
    asst = out[1]
    assert asst["role"] == "assistant"
    assert isinstance(asst["content"], list)
    assert asst["content"][0] == {"type": "text", "text": "hello"}


# ---------------------------------------------------------------------------
# Tools schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tools_to_anthropic_format_no_function_wrapper() -> None:
    """Anthropic tools 不含 function 包装，直接用 input_schema。"""
    tool = MagicMock()
    tool.name = "read_file"
    tool.description = "Read a file"
    tool.input_schema = {"type": "object", "properties": {"path": {"type": "string"}}}

    result = AnthropicMessagesProvider._tools_to_anthropic_format([tool])
    assert len(result) == 1
    assert result[0] == {
        "name": "read_file",
        "description": "Read a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
    assert "function" not in result[0]
    assert "type" not in result[0]


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_response_text_only() -> None:
    """纯文本响应解析正确。"""
    provider = _make_provider()
    data: dict[str, Any] = {
        "content": [{"type": "text", "text": "Hello!"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    resp = provider._parse_response(data)
    assert resp.message.content == "Hello!"
    assert resp.finish_reason == "stop"
    assert resp.usage is not None
    assert resp.usage.input_uncached_tokens.value == 10
    assert resp.usage.output_total_tokens.value == 5
    assert resp.usage.input_total_tokens.value is None
    assert resp.usage.total_tokens.value is None
    # provider_metadata 默认为空 dict（无扩展字段）
    assert isinstance(resp.provider_metadata, dict)


@pytest.mark.unit
def test_parse_response_cache_tokens_use_canonical_snapshot() -> None:
    """Anthropic cache token 字段进入 canonical usage snapshot。"""
    provider = _make_provider()
    data: dict[str, Any] = {
        "id": "msg_abc",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": "Hi"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 80,
            "cache_read_input_tokens": 30,
        },
    }
    resp = provider._parse_response(data)
    assert resp.usage is not None
    assert resp.usage.input_uncached_tokens.value == 100
    assert resp.usage.cache_write_tokens.value == 80
    assert resp.usage.cache_read_tokens.value == 30
    assert resp.usage.input_total_tokens.value == 210
    assert resp.usage.output_total_tokens.value == 20
    assert resp.usage.total_tokens.value == 230
    assert resp.provider_metadata["id"] == "msg_abc"
    assert resp.provider_metadata["model"] == "claude-sonnet-4-5"


@pytest.mark.unit
def test_parse_response_tool_use_block() -> None:
    """tool_use block 解析为 ToolCall。"""
    provider = _make_provider()
    data: dict[str, Any] = {
        "content": [
            {"type": "tool_use", "id": "tu_1", "name": "shell", "input": {"cmd": "ls"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 20, "output_tokens": 8},
    }
    resp = provider._parse_response(data)
    assert resp.finish_reason == "tool_calls"
    assert resp.message.tool_calls is not None
    assert len(resp.message.tool_calls) == 1
    tc = resp.message.tool_calls[0]
    assert tc.call_id == "tu_1"
    assert tc.tool_name == "shell"
    assert tc.arguments == {"cmd": "ls"}


@pytest.mark.unit
def test_parse_response_empty_content_raises() -> None:
    """空 content list 应抛 ProviderError。"""
    from core.errors import ProviderError

    provider = _make_provider()
    with pytest.raises(ProviderError):
        provider._parse_response({"content": [], "stop_reason": "end_turn"})


# ---------------------------------------------------------------------------
# stop_reason 映射
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw, has_tools, expected",
    [
        ("end_turn", False, "stop"),
        ("stop_sequence", False, "stop"),
        ("max_tokens", False, "length"),
        ("tool_use", False, "tool_calls"),
        ("tool_use", True, "tool_calls"),
        ("end_turn", True, "tool_calls"),  # has_tools 优先
        ("unknown_reason", False, "other"),
    ],
)
def test_normalize_stop_reason(raw: str, has_tools: bool, expected: str) -> None:
    result = AnthropicMessagesProvider._normalize_stop_reason(raw, message_has_tool_calls=has_tools)
    assert result == expected


# ---------------------------------------------------------------------------
# headers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_headers_contains_api_key_and_version() -> None:
    provider = _make_provider(api_key="sk-ant-abc123")
    headers = provider._build_headers()
    assert headers["x-api-key"] == "sk-ant-abc123"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["content-type"] == "application/json"
    assert "Authorization" not in headers


@pytest.mark.unit
def test_build_headers_uses_explicit_bearer_header() -> None:
    provider = _make_provider(
        api_key="sk-compatible",
        base_url="https://api.minimaxi.com/anthropic",
        api_key_header="authorization-bearer",
    )

    headers = provider._build_headers()

    assert headers["Authorization"] == "Bearer sk-compatible"
    assert "x-api-key" not in headers


@pytest.mark.unit
def test_build_headers_does_not_switch_by_host() -> None:
    provider = _make_provider(
        api_key="sk-compatible",
        base_url="https://api.minimaxi.com/anthropic",
    )

    headers = provider._build_headers()

    assert headers["x-api-key"] == "sk-compatible"
    assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# session_engine 分发
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MiniMax via AnthropicMessagesProvider
# ---------------------------------------------------------------------------


def _make_reasoning_provider(
    *,
    name: str = "MiniMax-M3",
    capability: ReasoningCapability | None = None,
) -> AnthropicMessagesProvider:
    """构造带 catalog capability 的 Anthropic-compatible provider。"""
    runtime, credential = make_model_runtime(
        protocol=ProviderProtocol.ANTHROPIC,
        name=name,
        base_url="https://api.anthropic.com",
        api_key="sk-ant-test",
        reasoning=capability,
        reasoning_effort="high" if capability is not None else None,
    )
    return AnthropicMessagesProvider(model_config=runtime, credential=credential)


@pytest.mark.unit
def test_minimax_profile_injects_thinking_toggle_via_build_payload() -> None:
    """MiniMax-M3 经 AnthropicMessagesProvider._build_payload 注入 thinking 开关。

    spec 验证项：MiniMax M3 只发送 thinking.type，不发送顶层 reasoning_effort。
    """
    provider = _make_reasoning_provider(capability=_minimax_reasoning_profile())
    request = LLMRequest(model="MiniMax-M3", messages=(Message.user("hi"),))
    payload = provider._build_payload(request)
    assert payload.get("thinking") == {"type": "adaptive"}
    assert "reasoning_effort" not in payload


@pytest.mark.unit
def test_minimax_request_reasoning_effort_overrides_config() -> None:
    """请求级 reasoning_effort 覆盖 Anthropic provider 配置默认值。"""
    provider = _make_reasoning_provider(capability=_minimax_reasoning_profile())
    request = LLMRequest(
        model="MiniMax-M3",
        messages=(Message.user("hi"),),
        reasoning_effort="low",
    )
    payload = provider._build_payload(request)
    assert payload.get("thinking") == {"type": "adaptive"}
    assert "reasoning_effort" not in payload


@pytest.mark.unit
def test_minimax_request_reasoning_none_disables_thinking() -> None:
    """MiniMax-M3 请求级 reasoning_effort='none' 映射到 thinking disabled。"""
    provider = _make_reasoning_provider(capability=_minimax_reasoning_profile())
    request = LLMRequest(
        model="MiniMax-M3",
        messages=(Message.user("hi"),),
        reasoning_effort="none",
    )
    payload = provider._build_payload(request)
    assert payload.get("thinking") == {"type": "disabled"}
    assert "reasoning_effort" not in payload


@pytest.mark.unit
def test_minimax_payload_logs_reasoning_decision(caplog: pytest.LogCaptureFixture) -> None:
    """MiniMax payload 构造记录 reasoning 开关、请求档位和归一档位。"""
    provider = _make_reasoning_provider(capability=_minimax_reasoning_profile())
    request = LLMRequest(model="MiniMax-M3", messages=(Message.user("hi"),))

    with caplog.at_level(logging.INFO, logger="infrastructure.llm_providers.anthropic_messages"):
        payload = provider._build_payload(request)

    assert payload.get("thinking") == {"type": "adaptive"}
    assert "llm.reasoning provider=anthropic" in caplog.text
    assert "switch=enabled" in caplog.text
    assert "requested_effort=high" in caplog.text
    assert "normalized_effort=high" in caplog.text
    assert "payload_keys=['thinking']" in caplog.text


@pytest.mark.unit
def test_minimax_profile_not_sent_when_capability_empty() -> None:
    """catalog 未声明 reasoning capability 时保持 payload 干净。"""
    provider = _make_reasoning_provider()
    request = LLMRequest(model="MiniMax-M3", messages=(Message.user("hi"),))
    payload = provider._build_payload(request)
    assert "reasoning_effort" not in payload


@pytest.mark.unit
def test_anthropic_model_without_capability_skips_reasoning() -> None:
    """模型 snapshot 无 capability 时不注入 reasoning 参数。"""
    provider = _make_reasoning_provider(name="claude-sonnet-4-5")
    request = LLMRequest(model="claude-sonnet-4-5", messages=(Message.user("hi"),))
    payload = provider._build_payload(request)
    assert "reasoning_effort" not in payload


@pytest.mark.unit
def test_deepseek_anthropic_profile_injects_output_config_effort() -> None:
    """DeepSeek Anthropic-compatible profile 注入 thinking + output_config.effort。"""
    capability = ReasoningCapability(
        adapter=ReasoningAdapter.DEEPSEEK_ANTHROPIC_THINKING,
        supported_efforts=("high", "max"),
        default_effort="high",
        supports_disabled=True,
        effort_map={"low": "high", "medium": "high", "high": "high", "max": "max"},
    )
    provider = _make_reasoning_provider(name="deepseek-v4-pro", capability=capability)
    request = LLMRequest(
        model="deepseek-v4-pro",
        messages=(Message.user("hi"),),
        reasoning_effort="max",
    )
    payload = provider._build_payload(request)
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["output_config"] == {"effort": "max"}
    assert "reasoning_effort" not in payload


@pytest.mark.unit
def test_deepseek_anthropic_low_maps_to_high() -> None:
    """DeepSeek Anthropic profile 按配置把 low 映射到 high。"""
    capability = ReasoningCapability(
        adapter=ReasoningAdapter.DEEPSEEK_ANTHROPIC_THINKING,
        supported_efforts=("high", "max"),
        default_effort="high",
        supports_disabled=True,
        effort_map={"low": "high", "medium": "high", "high": "high", "max": "max"},
    )
    provider = _make_reasoning_provider(name="deepseek-v4-pro", capability=capability)
    request = LLMRequest(
        model="deepseek-v4-pro",
        messages=(Message.user("hi"),),
        reasoning_effort="low",
    )
    payload = provider._build_payload(request)
    assert payload["output_config"] == {"effort": "high"}


@pytest.mark.unit
def test_native_runtime_dispatches_anthropic_provider() -> None:
    """provider=anthropic 时 SessionEngine.build 应使用 AnthropicMessagesProvider。"""
    cfg = Config(model=ModelSelectionConfig(preset_id="minimax-m3"))
    manager = ModelCatalogManager(environ={"MINIMAX_API_KEY": "sk-ant-test"})

    from runtime_assembly.session_engine import SessionEngine

    runtime = SessionEngine.build(cfg, model_catalog_manager=manager)
    assert runtime is not None
    # runner 内部持有 llm；通过 runner 的 _llm 属性验证类型
    assert isinstance(runtime._llm, AnthropicMessagesProvider)


@pytest.mark.unit
def test_native_runtime_dispatches_openai_provider_by_default() -> None:
    """provider=openai_compatible 时应使用 OpenAIResponsesProvider。"""
    from infrastructure.llm_providers.openai_responses import OpenAIResponsesProvider
    from runtime_assembly.session_engine import SessionEngine

    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    runtime = SessionEngine.build(cfg)
    assert isinstance(runtime._llm, OpenAIResponsesProvider)


# ---------------------------------------------------------------------------
# stream() e2e（httpx.MockTransport 伪造 SSE 字节流）
# ---------------------------------------------------------------------------


def _make_anthropic_sse_bytes(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    """把 (event_name, data_dict) 列表序列化为 Anthropic SSE 线传字节流。"""
    import json as _json

    parts: list[str] = []
    for name, data in events:
        parts.append(f"event: {name}\n")
        parts.append(f"data: {_json.dumps(data)}\n\n")
    return "".join(parts).encode("utf-8")


def _build_mock_transport(sse_bytes: bytes, *, status_code: int = 200) -> Any:
    """构造 httpx.MockTransport，固定返回给定 SSE 字节流。"""
    import httpx

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"content-type": "text/event-stream"},
            content=sse_bytes,
        )

    return httpx.MockTransport(_handler)


@pytest.mark.unit
async def test_stream_basic_text_block_e2e() -> None:
    """provider.stream() 端到端：MockTransport 喂入 SSE 字节流，收齐 LLMStreamChunk 序列。"""
    import httpx

    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_e2e",
                    "model": "claude-test",
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "你"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "好"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    sse = _make_anthropic_sse_bytes(events)
    transport = _build_mock_transport(sse)

    provider = _make_provider()
    # 注入 mock transport 作为 client 的运输层
    provider._client = httpx.AsyncClient(transport=transport)

    request = LLMRequest(model="claude-sonnet-4-5", messages=(Message.user("hi"),))
    chunks = [c async for c in provider.stream(request)]

    # 收尾必为 message.done
    assert chunks[-1].kind == "message.done"
    assert chunks[-1].message is not None
    assert chunks[-1].message.content == "你好"
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.input_uncached_tokens.value == 10
    assert chunks[-1].usage.output_total_tokens.value == 2
    assert chunks[-1].usage.input_total_tokens.value is None

    await provider.aclose()


@pytest.mark.unit
async def test_stream_http_error_raises_provider_error() -> None:
    """4xx HTTP 状态码 → ProviderError，details 包含 status_code / url / model。"""
    import httpx

    from core.errors import ProviderError

    transport = _build_mock_transport(b"upstream is down", status_code=503)
    provider = _make_provider()
    provider._client = httpx.AsyncClient(transport=transport)

    request = LLMRequest(model="claude-sonnet-4-5", messages=(Message.user("hi"),))
    with pytest.raises(ProviderError) as excinfo:
        async for _ in provider.stream(request):
            pass
    assert excinfo.value.details is not None
    assert excinfo.value.details.get("status_code") == 503
    assert "claude-sonnet-4-5" in excinfo.value.details.get("model", "")

    await provider.aclose()


@pytest.mark.unit
async def test_stream_tool_use_block_e2e() -> None:
    """tool_use block：start/arguments.delta/end 三元组顺序 + 流末 ToolCall 完整。"""
    import httpx

    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_tu",
                    "model": "claude-test",
                    "usage": {"input_tokens": 5, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "tu_1", "name": "Echo", "input": {}},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"msg":'},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '"hi"}'},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 4},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    sse = _make_anthropic_sse_bytes(events)
    provider = _make_provider()
    provider._client = httpx.AsyncClient(transport=_build_mock_transport(sse))

    request = LLMRequest(model="claude-sonnet-4-5", messages=(Message.user("call tool"),))
    chunks = [c async for c in provider.stream(request)]

    kinds = [c.kind for c in chunks]
    # 序列约束：tool_call.start 在 arguments.delta 之前；tool_call.end 在最后 delta 之后
    assert "tool_call.start" in kinds
    assert "tool_call.end" in kinds
    sidx = kinds.index("tool_call.start")
    eidx = kinds.index("tool_call.end")
    args_indices = [i for i, k in enumerate(kinds) if k == "tool_call.arguments.delta"]
    assert all(sidx < a < eidx for a in args_indices)

    last = chunks[-1]
    assert last.kind == "message.done"
    assert last.finish_reason == "tool_calls"
    assert last.message is not None and last.message.tool_calls is not None
    assert last.message.tool_calls[0].arguments == {"msg": "hi"}

    await provider.aclose()
