"""catalog runtime snapshot 到 provider payload 的薄合同测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.contracts import LLMRequest, LLMResponse
from core.message import Message
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config, ModelSelectionConfig
from infrastructure.llm_providers.openai_responses import OpenAIResponsesProvider
from infrastructure.tracing.trace_sink import JsonlTraceSink
from runtime_assembly.session_engine import SessionEngine


def _provider() -> OpenAIResponsesProvider:
    """从真实内置 catalog 解析 GLM runtime 并构造 provider。"""
    manager = ModelCatalogManager(environ={"GLM_API_KEY": "test-key"})
    runtime = manager.resolve_runtime(ModelSelectionConfig(preset_id="bigmodel-glm5-1m"))
    credential = manager.resolve_credential(runtime)
    return OpenAIResponsesProvider(model_config=runtime, credential=credential)


def test_glm_none_sends_explicit_disabled_payload() -> None:
    """请求级 none 必须显式关闭 GLM thinking。"""
    payload = _provider()._build_payload(
        LLMRequest(
            model=None,
            messages=(Message.user("hi"),),
            reasoning_effort="none",
        )
    )

    assert payload["model"] == "glm-5.2"
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


def test_glm_high_sends_enabled_and_reasoning_effort() -> None:
    """请求级 high 同时发送 GLM thinking 开关与档位。"""
    payload = _provider()._build_payload(
        LLMRequest(
            model=None,
            messages=(Message.user("hi"),),
            reasoning_effort="high",
        )
    )

    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"


class _TraceLLM:
    """让真实 Runner 产生 llm.request trace 的无网络 provider。"""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """返回固定终态响应。"""
        return LLMResponse(message=Message.assistant("ok"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effort", "normalized", "payload_keys"),
    [
        ("none", None, ["thinking"]),
        ("high", "high", ["reasoning_effort", "thinking"]),
    ],
)
async def test_glm_reasoning_decision_is_written_to_request_trace(
    tmp_path: Path,
    effort: str,
    normalized: str | None,
    payload_keys: list[str],
) -> None:
    """单次真实 runtime run 在 trace 留下 catalog 与 reasoning 脱敏证据。"""
    trace_path = tmp_path / f"trace-{effort}.jsonl"
    runtime = SessionEngine.build(
        Config(model=ModelSelectionConfig(preset_id="bigmodel-glm5-1m")),
        llm_provider=_TraceLLM(),
        model_catalog_manager=ModelCatalogManager(environ={}),
        event_sinks=[JsonlTraceSink(trace_path)],
    )

    result = await runtime.run("hello", session_id=f"trace-{effort}", reasoning_effort=effort)

    assert result.status == "completed"
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    request = next(event for event in events if event["kind"] == "llm.request")
    metadata = request["payload"]["request"]["metadata"]
    assert metadata["model_catalog"] == {
        "version": 2,
        "source": "builtin",
        "provider_id": "glm",
        "preset_id": "bigmodel-glm5-1m",
        "remote_model": "glm-5.2",
    }
    assert metadata["reasoning_plan"] == {
        "requested_effort": effort,
        "effective_effort": effort,
        "normalized_effort": normalized,
        "adapter": "glm_thinking_toggle",
        "send_reasoning": True,
        "payload_keys": payload_keys,
    }
