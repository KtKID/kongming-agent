"""LLM provider protocol and non-streaming request/response contracts."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from core.contracts.tool_runtime import Tool
from core.message import Message

# LLM Provider 相关支撑类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMRequest:
    """runner 发给 provider 的统一请求。

    Attributes:
        model: 模型名。核心协议不收模型厂商信息，只收字符串。
        messages: 对话历史片段，包含 system / user / assistant / tool。
        tools: 可用的工具协议列表；provider 负责转成自己的 schema。
        temperature / max_tokens / timeout_seconds: 采样参数；``None`` 表示由 provider
            默认或由统一配置层补齐。
        reasoning_effort: 统一 reasoning 档位。``None`` 表示本次请求未指定。
        metadata: 预留给装配层附加 provider 特定字段，不进核心协议。
    """

    model: str
    messages: tuple[Message, ...]
    tools: tuple[Tool, ...] = ()
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    reasoning_effort: str | None = None

    def to_audit_dict(self) -> dict[str, Any]:
        """返回可落盘的 LLMRequest 审计快照。"""
        return _audit_dataclass(self)


FinishReason = Literal["stop", "tool_calls", "length", "error", "other"]


@dataclass(frozen=True)
class LLMResponse:
    """provider 返回的统一响应。

    Attributes:
        message: 模型这一轮的 assistant 消息。允许只带 tool_calls，不带 content。
        finish_reason: 结束原因；tool_calls 表示模型要求继续执行工具。
        usage: token 使用量等运行时统计（透传给 infrastructure.tracing）。
        provider_metadata: 厂商扩展字段（reasoning_tokens / cached_tokens /
            reasoning_content / request_id / model / system_fingerprint 等）。
            dict 结构，保留原始字段名，不强行归一化。core 不解释内容，只透传。
        raw: 原始 provider 响应的精简引用，仅用于调试，core 不读它。
    """

    message: Message
    finish_reason: FinishReason = "stop"
    usage: dict[str, Any] = field(default_factory=dict)
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    raw: Any | None = None

    def to_audit_dict(self) -> dict[str, Any]:
        """返回可落盘的 LLMResponse 审计快照。"""
        return _audit_dataclass(self)


@runtime_checkable
class LLMProvider(Protocol):
    """LLM provider 统一协议。

    具体适配在 ``executors/llm/*.py``。
    """

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """发起一次模型调用并拿回统一响应。必须 async。"""
        ...


def _audit_dataclass(value: Any) -> dict[str, Any]:
    return {
        field.name: _audit_json_value(getattr(value, field.name))
        for field in dataclasses.fields(value)
    }


def _audit_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if _looks_like_tool(value):
        return {
            "name": _audit_json_value(value.name),
            "description": _audit_json_value(value.description),
            "input_schema": _audit_json_value(value.input_schema),
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _audit_dataclass(value)
    if isinstance(value, dict):
        return {str(key): _audit_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_json_value(item) for item in value]
    if isinstance(value, set):
        return [_audit_json_value(item) for item in sorted(value, key=repr)]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return repr(value)


def _looks_like_tool(value: Any) -> bool:
    return all(hasattr(value, attr) for attr in ("name", "description", "input_schema"))


# ---------------------------------------------------------------------------
__all__ = [
    "FinishReason",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
]
