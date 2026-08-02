"""e2e：evolution reviewer 工具合同经真实 Web 事件投影的端到端验证。

关键流程：
- 用流式 fake provider 驱动真实 SessionEngine、EvolutionManager 与 evolution_write。
- 首次 reviewer 响应生成大量未声明 documents 调用，验证首个 start 即熔断并纠错。
- 把 evolution event bus 接入真实 WSEventSink，验证非法工具帧为零且终态通知唯一。

关键函数：
- ``_review_arguments``：生成可真实写入临时 evolution store 的 reviewer 参数。
- ``_build_runtime``：装配真实主 runtime、EvolutionManager 和 Web sink 路由。
- 两个 e2e 用例分别验证纠错成功与纠错再次违规失败。
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from core.contracts import LLMRequest, LLMResponse, LLMStreamChunk
from core.message import Message, ToolCall
from evolution.evolution_manager import EvolutionManager
from evolution.lifecycle import register_evolution_lifecycle_hook
from hosts.web.websocket.event_sink import WSEventSink
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    EvolutionConfig,
    EvolutionLearningConfig,
    EvolutionMemoryConfig,
    FileToolConfig,
    ModelSelectionConfig,
    RunnerConfig,
    ShellToolConfig,
    ToolConfig,
)
from runtime_assembly.session_engine import SessionEngine
from tests.e2e.conftest import MemoryEventSink
from tools import ToolRegistry


class _FakeWebSocket:
    """记录真实 WSEventSink 输出的 wire payload。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        """记录一条 WebSocket JSON 帧。"""
        self.sent.append(dict(payload))

    async def close(self) -> None:
        """满足 WSEventSink 的关闭接口。"""
        return None


class _ScriptedStreamingProvider:
    """跨 parent/reviewer runtime 共享脚本的流式 fake provider。"""

    scripts: list[list[LLMStreamChunk]] = []
    requests: list[LLMRequest] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        """provider 工厂兼容入口。"""

    @classmethod
    def reset(cls, scripts: list[list[LLMStreamChunk]]) -> None:
        """重置响应脚本与请求记录。"""
        cls.scripts = [list(script) for script in scripts]
        cls.requests = []

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """返回当前请求独占的异步响应流。"""
        type(self).requests.append(request)
        chunks = type(self).scripts.pop(0)

        async def _iterate() -> AsyncIterator[LLMStreamChunk]:
            """逐个产出脚本 chunk，支持 Runner 调用 ``aclose``。"""
            for chunk in chunks:
                yield chunk

        return _iterate()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """流式配置下禁止回落到 complete。"""
        raise AssertionError(f"unexpected non-stream request: {request.model}")

    async def aclose(self) -> None:
        """满足 SessionEngine 的资源关闭接口。"""
        return None


def _block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """阻止测试访问真实网络。"""

    def _deny_network(*_args: object, **_kwargs: object) -> None:
        """任何 socket 连接都表明 fake provider 装配失效。"""
        raise AssertionError("evolution guard e2e attempted an external network call")

    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)


def _cfg(tmp_path: Path) -> Config:
    """生成每次主 run 都触发 reviewer 的临时配置。"""
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        runner=RunnerConfig(max_turns=4),
        approval=ApprovalConfig(mode="auto_allow"),
        tool=ToolConfig(
            file=FileToolConfig(enabled=False),
            shell=ShellToolConfig(enabled=False),
        ),
        evolution=EvolutionConfig(
            memory=EvolutionMemoryConfig(enabled=False),
            learning=EvolutionLearningConfig(
                enabled=True,
                auto_trigger_enabled=True,
                preset_id="bigmodel-glm5-1m",
                reasoning_effort="low",
                every_n_runs=1,
                min_user_turns=1,
                max_history_messages=10,
                max_nutrients=1,
                nutrient_confidence_threshold=0.75,
                review_timeout_seconds=1.0,
                drain_on_close_seconds=1.0,
                root_path=str(tmp_path / ".kongming" / "evolution"),
            ),
        ),
    )


def _done(message: Message, *, finish_reason: str) -> LLMStreamChunk:
    """构造一个 message.done chunk。"""
    return LLMStreamChunk(
        kind="message.done",
        message=message,
        finish_reason=finish_reason,  # type: ignore[arg-type]
    )


def _invalid_documents_stream(count: int) -> list[LLMStreamChunk]:
    """生成大量未声明 documents 调用；Runner 应在首个 start 关闭响应。"""
    calls = [
        ToolCall(
            call_id=f"documents-{index}",
            tool_name="documents",
            arguments={"path": f"/sensitive/{index}"},
        )
        for index in range(count)
    ]
    return [
        *[
            LLMStreamChunk(
                kind="tool_call.start",
                index=index,
                tool_call_id=call.call_id,
                tool_name=call.tool_name,
            )
            for index, call in enumerate(calls)
        ],
        _done(Message.assistant(None, tool_calls=calls), finish_reason="tool_calls"),
    ]


def _review_arguments(*, session_id: str, parent_run_id: str) -> dict[str, Any]:
    """生成 evolution_write 可落盘的合法参数。"""
    return {
        "review_result": {
            "run_id": parent_run_id,
            "session_id": session_id,
            "reviewed_at_ms": 1234567890,
            "review_summary": "captured one guarded workflow nutrient",
            "nutrients": [
                {
                    "nutrient_id": "nutrient-tool-guard-1",
                    "kind": "workflow",
                    "title": "guard reviewer tools",
                    "content": "Reject undeclared reviewer tools before any execution.",
                    "summary": "guard reviewer tool calls",
                    "confidence": 0.95,
                    "evidence_turns": [1],
                    "source_run_id": parent_run_id,
                    "source_session_id": session_id,
                    "suggested_target": "skill",
                    "tags": ["evolution", "guard"],
                }
            ],
            "skip_reasons": [],
        },
        "trigger_reason": "cadence",
        "transcript_window": {
            "session_id": session_id,
            "run_id": parent_run_id,
            "user_turn_count": 1,
            "included_turns": [1],
            "messages": [{"turn": 1, "role": "user", "content": "review this"}],
            "final_message": "parent done",
            "tool_call_count": 0,
            "summary": "one turn",
        },
    }


def _valid_write_stream(*, session_id: str, parent_run_id: str) -> list[LLMStreamChunk]:
    """生成一次合法 evolution_write 流式响应。"""
    call = ToolCall(
        call_id="evolution-write-1",
        tool_name="evolution_write",
        arguments=_review_arguments(session_id=session_id, parent_run_id=parent_run_id),
    )
    return [
        LLMStreamChunk(
            kind="tool_call.start",
            index=0,
            tool_call_id=call.call_id,
            tool_name=call.tool_name,
        ),
        _done(Message.assistant(None, tool_calls=[call]), finish_reason="tool_calls"),
    ]


def _build_runtime(
    *,
    cfg: Config,
    tmp_path: Path,
    session_id: str,
) -> tuple[SessionEngine, EvolutionManager, MemoryEventSink, _FakeWebSocket]:
    """装配真实 Manager、runtime、event bus 和 Web sink。"""
    memory_sink = MemoryEventSink()
    websocket = _FakeWebSocket()
    web_sink = WSEventSink(websocket, thread_id=session_id)
    registry = ToolRegistry()
    manager = EvolutionManager(config=cfg, kongming_home=tmp_path)
    manager.register_runtime_tools(registry, event_sinks=[memory_sink])
    manager.register_event_route(session_id, web_sink)
    runtime = SessionEngine.build(
        cfg,
        tools=registry,
        enabled_tool_names=[],
        event_sinks=[memory_sink],
    )
    register_evolution_lifecycle_hook(runtime=runtime, manager=manager)
    return runtime, manager, memory_sink, websocket


@pytest.mark.e2e
async def test_evolution_guard_retries_185_documents_and_emits_one_completed_notice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """首个非法调用熔断，纠错成功后只写一份 review。"""
    _block_external_network(monkeypatch)
    monkeypatch.setattr(
        "runtime_assembly.session_engine.build_provider",
        lambda *_args, **_kwargs: _ScriptedStreamingProvider(),
    )
    session_id = "thread-abcdef123456"
    parent_run_id = f"run-{session_id}-1"
    _ScriptedStreamingProvider.reset(
        [
            [
                LLMStreamChunk(kind="content.delta", delta="parent done"),
                _done(Message.assistant("parent done"), finish_reason="stop"),
            ],
            _invalid_documents_stream(185),
            _valid_write_stream(session_id=session_id, parent_run_id=parent_run_id),
        ]
    )
    cfg = _cfg(tmp_path)
    runtime, manager, memory_sink, websocket = _build_runtime(
        cfg=cfg,
        tmp_path=tmp_path,
        session_id=session_id,
    )

    with caplog.at_level(logging.ERROR, logger="evolution.trigger_diagnostics"):
        result = await runtime.run("review this", session_id=session_id)
        await manager.aclose()
    await runtime.aclose()

    assert result.status == "completed"
    assert len(_ScriptedStreamingProvider.requests) == 3
    correction = _ScriptedStreamingProvider.requests[2].messages[-1]
    assert correction.role == "user"
    assert "documents" in (correction.content or "")
    assert "evolution_write" in (correction.content or "")
    assert "category=reviewer_tool_contract_violation" in caplog.text
    assert "tool=documents" in caplog.text
    assert "/sensitive/" not in caplog.text
    assert caplog.text.count("category=reviewer_tool_contract_violation") == 1

    review_dir = tmp_path / ".kongming" / "evolution" / "reviews"
    review_files = list(review_dir.glob("*.json"))
    assert len(review_files) == 1
    review = json.loads(review_files[0].read_text(encoding="utf-8"))
    assert review["run_id"] == parent_run_id

    illegal_events = [
        event
        for event in memory_sink.events
        if event.kind in {"tool.call.start", "tool.call.end"}
        and event.payload.get("tool_name") == "documents"
    ]
    assert illegal_events == []
    tool_frames = [
        frame
        for frame in websocket.sent
        if frame["frame_type"] in {"tool.call.start", "tool.call.end"}
    ]
    assert [frame["frame_type"] for frame in tool_frames] == [
        "tool.call.start",
        "tool.call.end",
    ]
    assert tool_frames[0]["tool_name"] == "evolution_write"
    completed = [
        frame
        for frame in websocket.sent
        if frame["frame_type"] == "system.notice" and frame["status"] == "completed"
    ]
    failed = [
        frame
        for frame in websocket.sent
        if frame["frame_type"] == "system.notice" and frame["status"] == "failed"
    ]
    assert len(completed) == 1
    assert failed == []


@pytest.mark.e2e
async def test_evolution_guard_second_violation_emits_one_failed_notice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """纠错响应再次违规时只产生一个 failed 终态。"""
    _block_external_network(monkeypatch)
    monkeypatch.setattr(
        "runtime_assembly.session_engine.build_provider",
        lambda *_args, **_kwargs: _ScriptedStreamingProvider(),
    )
    session_id = "thread-fedcba654321"
    _ScriptedStreamingProvider.reset(
        [
            [
                LLMStreamChunk(kind="content.delta", delta="parent done"),
                _done(Message.assistant("parent done"), finish_reason="stop"),
            ],
            _invalid_documents_stream(185),
            _invalid_documents_stream(185),
        ]
    )
    cfg = _cfg(tmp_path)
    runtime, manager, memory_sink, websocket = _build_runtime(
        cfg=cfg,
        tmp_path=tmp_path,
        session_id=session_id,
    )

    with caplog.at_level(logging.ERROR, logger="evolution.trigger_diagnostics"):
        result = await runtime.run("review this", session_id=session_id)
        await manager.aclose()
    await runtime.aclose()

    assert result.status == "completed"
    assert len(_ScriptedStreamingProvider.requests) == 3
    assert not (tmp_path / ".kongming" / "evolution" / "reviews").exists()
    illegal_events = [
        event
        for event in memory_sink.events
        if event.kind in {"tool.call.start", "tool.call.end"}
        and event.payload.get("tool_name") == "documents"
    ]
    assert illegal_events == []
    tool_frames = [
        frame
        for frame in websocket.sent
        if frame["frame_type"] in {"tool.call.start", "tool.call.end"}
    ]
    assert tool_frames == []
    assert caplog.text.count("category=reviewer_tool_contract_violation") == 2
    completed = [
        frame
        for frame in websocket.sent
        if frame["frame_type"] == "system.notice" and frame["status"] == "completed"
    ]
    failed = [
        frame
        for frame in websocket.sent
        if frame["frame_type"] == "system.notice" and frame["status"] == "failed"
    ]
    assert completed == []
    assert len(failed) == 1
    assert failed[0]["details"]["error_kind"] == "LLMToolCallContractError"
