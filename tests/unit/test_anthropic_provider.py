"""unit：AnthropicMessagesProvider 消息格式转换 + 分发逻辑。

覆盖：
- _split_system_and_convert：system 提取、tool_result 合并、tool_use blocks
- _tools_to_anthropic_format：tools schema 无 function 包装
- _parse_response：stop_reason 映射、usage 归一化、tool_use block 解析
- _normalize_stop_reason：各种 stop_reason 映射
- native_runtime 分发：provider=anthropic → AnthropicMessagesProvider
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from config_loader.models import Config, ModelConfig, ReasoningProfile
from core.contracts import LLMRequest
from core.message import Message, ToolCall
from executors.llm.anthropic_messages import AnthropicMessagesProvider

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_provider(
    api_key: str = "sk-ant-test",
    base_url: str = "https://api.anthropic.com",
    name: str = "claude-sonnet-4-5",
) -> AnthropicMessagesProvider:
    return AnthropicMessagesProvider(
        model_config=ModelConfig(
            provider="anthropic",
            name=name,
            base_url=base_url,
            api_key=api_key,
        )
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
    assert resp.usage["prompt_tokens"] == 10
    assert resp.usage["completion_tokens"] == 5
    assert resp.usage["total_tokens"] == 15
    # provider_metadata 默认为空 dict（无扩展字段）
    assert isinstance(resp.provider_metadata, dict)


@pytest.mark.unit
def test_parse_response_provider_metadata_cache_tokens() -> None:
    """Anthropic cache token 字段被提取到 provider_metadata。"""
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
    assert resp.provider_metadata["cache_creation_input_tokens"] == 80
    assert resp.provider_metadata["cache_read_input_tokens"] == 30
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


# ---------------------------------------------------------------------------
# native_runtime 分发
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MiniMax via AnthropicMessagesProvider
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_minimax_profile_injects_reasoning_effort_via_build_payload() -> None:
    """MiniMax-M2.7* 经 AnthropicMessagesProvider._build_payload 注入 reasoning_effort。

    spec 验证项 #4：MiniMax-M2.7* 只在 Anthropic-compatible 路径下发送白名单字段。
    """
    provider = AnthropicMessagesProvider(
        model_config=ModelConfig(
            provider="anthropic",
            name="MiniMax-M2.7-3B",
            base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
            reasoning_effort="medium",
            reasoning_profiles={
                "MiniMax-M2.7": ReasoningProfile(
                    match="prefix",
                    adapter="anthropic_compatible_reasoning",
                    supported_efforts=["low", "medium", "high"],
                )
            },
        )
    )
    request = LLMRequest(model="MiniMax-M2.7-3B", messages=(Message.user("hi"),))
    payload = provider._build_payload(request)
    assert payload.get("reasoning_effort") == "medium"


@pytest.mark.unit
def test_minimax_profile_not_sent_when_profiles_empty() -> None:
    """reasoning_profiles 为空时，Anthropic provider 静默跳过 reasoning（有意设计）。"""
    provider = AnthropicMessagesProvider(
        model_config=ModelConfig(
            provider="anthropic",
            name="MiniMax-M2.7-3B",
            base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
            reasoning_effort="medium",
            # reasoning_profiles 未配置 → 静默跳过
        )
    )
    request = LLMRequest(model="MiniMax-M2.7-3B", messages=(Message.user("hi"),))
    payload = provider._build_payload(request)
    assert "reasoning_effort" not in payload


@pytest.mark.unit
def test_non_minimax_anthropic_model_not_sent_reasoning() -> None:
    """非白名单模型（如 claude-sonnet）无 profile 命中，不注入 reasoning 参数。"""
    provider = AnthropicMessagesProvider(
        model_config=ModelConfig(
            provider="anthropic",
            name="claude-sonnet-4-5",
            base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
            reasoning_effort="high",
            reasoning_profiles={
                "MiniMax-M2.7": ReasoningProfile(
                    match="prefix",
                    adapter="anthropic_compatible_reasoning",
                    supported_efforts=["low", "medium", "high"],
                )
            },
        )
    )
    # request.model 是 claude-sonnet，不命中 MiniMax-M2.7 prefix
    request = LLMRequest(model="claude-sonnet-4-5", messages=(Message.user("hi"),))
    payload = provider._build_payload(request)
    assert "reasoning_effort" not in payload


@pytest.mark.unit
def test_native_runtime_dispatches_anthropic_provider() -> None:
    """provider=anthropic 时 NativeRuntime.build 应使用 AnthropicMessagesProvider。"""
    cfg = Config(
        model=ModelConfig(
            provider="anthropic",
            name="claude-sonnet-4-5",
            base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
        )
    )

    from executors.agent_runtime.native_runtime import NativeRuntime

    runtime = NativeRuntime.build(cfg)
    assert runtime is not None
    # runner 内部持有 llm；通过 runner 的 _llm 属性验证类型
    assert isinstance(runtime._llm, AnthropicMessagesProvider)


@pytest.mark.unit
def test_native_runtime_dispatches_openai_provider_by_default() -> None:
    """provider=openai_compatible 时应使用 OpenAIResponsesProvider。"""
    from config_loader.models import Config, ModelConfig
    from executors.agent_runtime.native_runtime import NativeRuntime
    from executors.llm.openai_responses import OpenAIResponsesProvider

    cfg = Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="gemma-4-e4b-it",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        )
    )
    runtime = NativeRuntime.build(cfg)
    assert isinstance(runtime._llm, OpenAIResponsesProvider)
