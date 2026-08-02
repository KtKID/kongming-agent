"""e2e：self-evolution v0.1.9 主链。

覆盖 `SessionEngine.run() -> child reviewer -> evolution_write -> evo 落盘`
这条完整链路，同时验证 child reviewer 可配置独立模型名。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from core.contracts import LLMRequest, LLMResponse
from core.message import Message, ToolCall
from core.session import InMemorySession
from evolution.evolution_manager import EvolutionManager
from evolution.lifecycle import register_evolution_lifecycle_hook
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
from tests.e2e.conftest import MemoryEventSink, RecordingApproval
from tools import ToolRegistry


def _block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """阻断测试进程的外部 socket 连接，确保进化 e2e 只使用 fake provider。"""

    def _deny_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("self-evolution e2e attempted an external network call")

    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)


class _ScriptedOpenAIProvider:
    responses: list[tuple[str | None, list[ToolCall] | None]] = []
    seen_models: list[str] = []
    seen_requests: list[LLMRequest] = []

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        pass

    @classmethod
    def reset(cls) -> None:
        cls.responses = []
        cls.seen_models = []
        cls.seen_requests = []

    @classmethod
    def script(
        cls, *, content: str | None = None, tool_calls: list[ToolCall] | None = None
    ) -> None:
        cls.responses.append((content, tool_calls))

    async def complete(self, request: LLMRequest) -> LLMResponse:
        type(self).seen_models.append(request.model)
        type(self).seen_requests.append(request)
        if not type(self).responses:
            return LLMResponse(message=Message.assistant(""), finish_reason="stop")
        content, tool_calls = type(self).responses.pop(0)
        return LLMResponse(
            message=Message.assistant(content, tool_calls=tool_calls),
            finish_reason="tool_calls" if tool_calls else "stop",
        )

    async def aclose(self) -> None:
        return None


def _cfg(tmp_path: Path, *, auto_trigger_enabled: bool = True) -> Config:
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
                auto_trigger_enabled=auto_trigger_enabled,
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


@pytest.mark.e2e
async def test_self_evolution_run_child_reviewer_and_write_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _block_external_network(monkeypatch)
    monkeypatch.setattr(
        "runtime_assembly.session_engine.build_provider",
        lambda *_args, **_kwargs: _ScriptedOpenAIProvider(),
    )
    _ScriptedOpenAIProvider.reset()

    session_id = "e2e-evolution"
    parent_run_id = f"run-{session_id}-1"
    _ScriptedOpenAIProvider.script(content="parent done")
    _ScriptedOpenAIProvider.script(
        tool_calls=[
            ToolCall(
                call_id="evo-call-1",
                tool_name="evolution_write",
                arguments={
                    "review_result": {
                        "run_id": parent_run_id,
                        "session_id": session_id,
                        "reviewed_at_ms": 1234567890,
                        "review_summary": "captured one workflow nutrient",
                        "nutrients": [
                            {
                                "nutrient_id": "nutrient-e2e-1",
                                "kind": "workflow",
                                "title": "post-run review",
                                "content": "After each completed run, review recent turns and store reusable workflow knowledge.",
                                "summary": "post-run workflow nutrient",
                                "confidence": 0.92,
                                "evidence_turns": [1],
                                "source_run_id": parent_run_id,
                                "source_session_id": session_id,
                                "suggested_target": "skill",
                                "tags": ["workflow", "evolution"],
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
                        "messages": [
                            {"turn": 1, "role": "user", "content": "please solve this"},
                            {"turn": 1, "role": "assistant", "content": "parent done"},
                        ],
                        "final_message": "parent done",
                        "tool_call_count": 0,
                        "summary": "2 messages across 1 turns",
                    },
                },
            )
        ]
    )
    cfg = _cfg(tmp_path)
    sink = MemoryEventSink()
    registry = ToolRegistry()
    manager = EvolutionManager(config=cfg, kongming_home=tmp_path)
    manager.register_runtime_tools(
        registry,
        event_sinks=[sink],
    )
    runtime = SessionEngine.build(
        cfg,
        tools=registry,
        enabled_tool_names=[],
        event_sinks=[sink],
    )
    register_evolution_lifecycle_hook(runtime=runtime, manager=manager)

    result = await runtime.run("please solve this", session_id=session_id)
    await manager.aclose()
    await runtime.aclose()

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "parent done"
    assert _ScriptedOpenAIProvider.seen_models == [
        "gemma-4-e4b-it",
        "glm-5.2",
    ]

    evo_root = tmp_path / ".kongming" / "evolution"
    review_path = evo_root / "reviews" / f"{parent_run_id}.json"
    queue_path = evo_root / "evolution-nutrients.jsonl"
    state_path = evo_root / "evolution.state.json"
    assert review_path.exists()
    assert queue_path.exists()
    assert state_path.exists()

    review_data = json.loads(review_path.read_text(encoding="utf-8"))
    assert review_data["run_id"] == parent_run_id
    assert review_data["result"]["review_summary"] == "captured one workflow nutrient"

    queue_lines = queue_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(queue_lines) == 1
    queue_item = json.loads(queue_lines[0])
    assert queue_item["nutrient_id"] == "nutrient-e2e-1"
    assert queue_item["kind"] == "workflow"

    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    session_state = state_data["sessions"][session_id]
    # run_count 真源已迁到 session manifest；evolution state 只保留旁路元数据。
    assert session_state["run_count"] == 0
    assert session_state["user_turn_count"] == 1
    assert session_state["last_reviewed_run_id"] == parent_run_id
    assert session_state["last_nutrient_id"] == "nutrient-e2e-1"
    assert session_state["last_review_status"] == "written"

    event_kinds = sink.kinds()
    assert "evolution.review.started" in event_kinds
    assert "evolution.review.completed" in event_kinds
    assert "evolution.nutrient_written" in event_kinds


@pytest.mark.e2e
async def test_explicit_review_tool_runs_after_final_answer_in_manual_only_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """公开 Tool 经主 Runner 登记，最终回答后仅启动一次 child review。"""
    _block_external_network(monkeypatch)
    monkeypatch.setattr(
        "runtime_assembly.session_engine.build_provider",
        lambda *_args, **_kwargs: _ScriptedOpenAIProvider(),
    )
    _ScriptedOpenAIProvider.reset()

    session_id = "e2e-explicit-evolution"
    parent_run_id = f"run-{session_id}-1"
    focus = "重点提炼工具失败后的恢复流程"
    _ScriptedOpenAIProvider.script(
        tool_calls=[
            ToolCall(
                call_id="request-review-1",
                tool_name="request_evolution_review",
                arguments={"focus": focus},
            )
        ]
    )
    _ScriptedOpenAIProvider.script(content="parent final answer")
    _ScriptedOpenAIProvider.script(
        tool_calls=[
            ToolCall(
                call_id="evo-write-manual-1",
                tool_name="evolution_write",
                arguments={
                    "review_result": {
                        "run_id": parent_run_id,
                        "session_id": session_id,
                        "reviewed_at_ms": 1234567891,
                        "review_summary": "captured explicit recovery workflow",
                        "nutrients": [
                            {
                                "nutrient_id": "nutrient-manual-1",
                                "kind": "workflow",
                                "title": "recover after tool failure",
                                "content": "Inspect the structured tool error, correct inputs, and retry once.",
                                "summary": "tool failure recovery workflow",
                                "confidence": 0.93,
                                "evidence_turns": [1],
                                "source_run_id": parent_run_id,
                                "source_session_id": session_id,
                                "suggested_target": "skill",
                                "tags": ["workflow", "recovery"],
                            }
                        ],
                        "skip_reasons": [],
                    },
                    "trigger_reason": "manual_tool",
                    "transcript_window": {
                        "session_id": session_id,
                        "run_id": parent_run_id,
                        "user_turn_count": 1,
                        "included_turns": [1],
                        "messages": [
                            {
                                "turn": 1,
                                "role": "user",
                                "content": "review this run",
                            },
                            {
                                "turn": 2,
                                "role": "assistant",
                                "content": "parent final answer",
                            },
                        ],
                        "final_message": "parent final answer",
                        "tool_call_count": 1,
                        "summary": "explicit review request and final answer",
                    },
                },
            )
        ]
    )
    cfg = _cfg(tmp_path, auto_trigger_enabled=False)
    sink = MemoryEventSink()
    approval = RecordingApproval()
    session = InMemorySession(session_id=session_id)
    registry = ToolRegistry()
    manager = EvolutionManager(config=cfg, kongming_home=tmp_path)
    manager.register_runtime_tools(registry, event_sinks=[sink])
    runtime = SessionEngine.build(
        cfg,
        tools=registry,
        enabled_tool_names=["request_evolution_review"],
        event_sinks=[sink],
        approval=approval,
        session_factory=lambda _session_id: session,
    )
    register_evolution_lifecycle_hook(runtime=runtime, manager=manager)

    result = await runtime.run("review this run", session_id=session_id)
    await manager.aclose()
    history = await session.history()
    await runtime.aclose()

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "parent final answer"
    request_results = [
        message
        for message in history
        if message.role == "tool" and message.name == "request_evolution_review"
    ]
    assert len(request_results) == 1
    assert request_results[0].metadata["ok"] is True, request_results[0].metadata
    assert request_results[0].metadata["data"]["status"] == "queued"
    assert [request.tool_name for request in approval.requests] == ["request_evolution_review"]
    assert _ScriptedOpenAIProvider.seen_models == [
        "gemma-4-e4b-it",
        "gemma-4-e4b-it",
        "glm-5.2",
    ]
    reviewer_prompt = _ScriptedOpenAIProvider.seen_requests[2].messages[-1].content
    assert reviewer_prompt is not None
    assert focus in reviewer_prompt
    assert "触发原因: manual_tool" in reviewer_prompt
    assert any(
        message.content is not None and "[turn 1][assistant] parent final answer" in message.content
        for message in _ScriptedOpenAIProvider.seen_requests[2].messages
    )

    review_path = tmp_path / ".kongming" / "evolution" / "reviews" / f"{parent_run_id}.json"
    review_data = json.loads(review_path.read_text(encoding="utf-8"))
    assert review_data["trigger_reason"] == "manual_tool"
    assert review_data["transcript_window"]["included_turns"] == [1]
    assert len(list((tmp_path / ".kongming" / "evolution" / "reviews").glob("*.json"))) == 1

    request_start_events = [
        event
        for event in sink.events
        if event.kind == "tool.call.start"
        and event.payload.get("tool_name") == "request_evolution_review"
    ]
    request_end_events = [
        event
        for event in sink.events
        if event.kind == "tool.call.end"
        and event.payload.get("tool_name") == "request_evolution_review"
    ]
    assert len(request_start_events) == 1
    assert len(request_end_events) == 1
