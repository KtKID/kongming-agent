"""provider usage canonical snapshot 跨层集成测试。

本文件用 httpx MockTransport 替换外部 Anthropic HTTP 服务，仓库内真实链路保持
AnthropicMessagesProvider → Runner → FileSession/JsonlTraceSink/WSEventSink →
UsageTokenManager。关键测试验证 MiniMax 终态 usage 在各层数值一致，未知字段保持
None，并保留 provider raw usage 与 response identity。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx

from core import AgentSpec, Runner
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    ProviderUsageSnapshot,
)
from hosts.web.usage.usage_token_v2 import (
    GenericChatAnthropicUsage,
    UsageTokenManager,
)
from hosts.web.websocket.event_sink import WSEventSink
from infrastructure.config.model_provider_catalog import ProviderProtocol
from infrastructure.llm_providers.anthropic_messages import AnthropicMessagesProvider
from infrastructure.tracing import JsonlTraceSink
from sessions.file_session import FileSession
from sessions.session_bootstrap import SessionBootstrap
from tests._helpers.model_runtime import make_model_runtime


class _AllowApproval:
    """集成测试审批桩，所有工具调用均批准。"""

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """批准请求，输入为审批合同，输出为 approved decision。"""
        del request
        return ApprovalDecision(outcome="approved")


class _MetaReader:
    """把目标 thread 固定标记为 generic_chat。"""

    async def read(self, thread_id: str) -> dict[str, Any] | None:
        """读取 thread metadata，输入为 thread id，输出为 backend 标记。"""
        return {"backend_kind": "generic_chat"} if thread_id == "usage-flow" else None


class _EmptyLocator:
    """Claude/Codex locator 空实现。"""

    async def locate(self, thread_id: str) -> Path | None:
        """返回空路径，输入为 thread id，输出为 None。"""
        del thread_id
        return None


class _GenericLocator:
    """generic_chat FileSession 路径定位器。"""

    def __init__(self, path: Path) -> None:
        """保存 JSONL 路径，输入为 path，输出为 locator 实例。"""
        self._path = path

    async def locate(self, thread_id: str) -> Path | None:
        """定位目标 thread，输入为 thread id，输出为 JSONL 路径。"""
        return self._path if thread_id == "usage-flow" else None


def _bootstrap() -> SessionBootstrap:
    """构造 FileSession bootstrap，输入为空，输出为冻结启动快照。"""
    return SessionBootstrap(
        agent_name="usage-flow",
        model_name="MiniMax-M3",
        instruction_sources=["integration"],
        instruction_text_hash="sha256:usage-flow",
        instruction_text="system",
        created_at=1.0,
        cwd="/tmp",
        app_version="test",
    )


def _anthropic_response(request: httpx.Request) -> httpx.Response:
    """返回 MiniMax 形态响应，输入为 HTTP request，输出为 mock response。"""
    return httpx.Response(
        200,
        request=request,
        json={
            "id": "msg_minimax_cache_hit",
            "model": "MiniMax-M3",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 194,
                "output_tokens": 119,
                "cache_read_input_tokens": 9088,
                "vendor_extra": {"request_marker": "usage-flow"},
            },
        },
    )


async def test_minimax_usage_snapshot_matches_session_trace_web_and_query(
    tmp_path: Path,
) -> None:
    """同一 response snapshot 贯穿 Session、trace、WS 与查询门户。"""
    runtime, credential = make_model_runtime(
        protocol=ProviderProtocol.ANTHROPIC,
        name="MiniMax-M3",
        base_url="https://minimax.test",
        api_key="test-key",
    )
    provider = AnthropicMessagesProvider(
        model_config=runtime,
        credential=credential,
        max_retries=0,
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_anthropic_response))

    session = FileSession("usage-flow", _bootstrap(), str(tmp_path / "sessions"))
    trace_path = tmp_path / "trace.jsonl"
    websocket = AsyncMock()
    websocket.send_json = AsyncMock(return_value=None)
    runner = Runner(
        stream_enabled=False,
        event_sinks=[
            JsonlTraceSink(trace_path),
            WSEventSink(websocket, thread_id="usage-flow"),
        ],
    )

    try:
        result = await runner.run(
            "hello",
            session=session,
            agent_spec=AgentSpec(
                name="usage-flow",
                instructions="system",
                default_model="MiniMax-M3",
            ),
            llm=provider,
            tools={},
            approval=_AllowApproval(),
        )
    finally:
        await provider.aclose()

    assert result.status == "completed"
    session_path = tmp_path / "sessions" / "usage-flow" / "usage-flow.jsonl"
    records = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines()]
    assistant_usage_payload = records[-1]["usage"]
    assistant_usage = ProviderUsageSnapshot.from_payload(assistant_usage_payload)
    assert assistant_usage.provider_response_id == "msg_minimax_cache_hit"
    assert assistant_usage.input_uncached_tokens.value == 194
    assert assistant_usage.cache_read_tokens.value == 9088
    assert assistant_usage.cache_write_tokens.value is None
    assert assistant_usage.input_total_tokens.value is None
    assert assistant_usage.output_total_tokens.value == 119
    assert assistant_usage.raw_usage["vendor_extra"] == {"request_marker": "usage-flow"}

    trace_records = [
        json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    usage_event = next(record for record in trace_records if record["kind"] == "usage")
    assert usage_event["payload"] == assistant_usage_payload

    usage_frame = next(
        call.args[0]
        for call in websocket.send_json.await_args_list
        if call.args[0]["frame_type"] == "usage"
    )
    assert usage_frame["usage"]["input_tokens"] == 194
    assert usage_frame["usage"]["cache_read_input_tokens"] == 9088
    assert usage_frame["usage"]["cache_creation_input_tokens"] is None
    assert usage_frame["usage"]["context_usage"] is None

    usage_manager = UsageTokenManager(
        meta_reader=_MetaReader(),
        claude_locator=_EmptyLocator(),
        codex_locator=_EmptyLocator(),
        generic_locator=_GenericLocator(session_path),
    )
    queried = await usage_manager.get_thread_usage("usage-flow")
    assert isinstance(queried, GenericChatAnthropicUsage)
    assert queried.input_tokens == usage_frame["usage"]["input_tokens"]
    assert queried.cache_read_input_tokens == usage_frame["usage"]["cache_read_input_tokens"]
    assert queried.cache_creation_input_tokens is None
    assert queried.context_usage is None
