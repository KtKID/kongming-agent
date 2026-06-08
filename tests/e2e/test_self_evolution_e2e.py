"""e2e：self-evolution v0.1.9 主链。

覆盖 `NativeRuntime.run() -> child reviewer -> evolution_write -> evo 落盘`
这条完整链路，同时验证 child reviewer 可配置独立模型名。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.contracts import LLMRequest, LLMResponse
from core.message import Message, ToolCall
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    EvolutionConfig,
    EvolutionLearningConfig,
    EvolutionMemoryConfig,
    FileToolConfig,
    ModelConfig,
    RunnerConfig,
    ShellToolConfig,
    ToolConfig,
)
from runtime_assembly.native_runtime import NativeRuntime
from tests.e2e.conftest import MemoryEventSink
from tools import ToolRegistry, register_evolution_write_tool_if_enabled


class _ScriptedOpenAIProvider:
    responses: list[tuple[str | None, list[ToolCall] | None]] = []
    seen_models: list[str] = []

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        pass

    @classmethod
    def reset(cls) -> None:
        cls.responses = []
        cls.seen_models = []

    @classmethod
    def script(
        cls, *, content: str | None = None, tool_calls: list[ToolCall] | None = None
    ) -> None:
        cls.responses.append((content, tool_calls))

    async def complete(self, request: LLMRequest) -> LLMResponse:
        type(self).seen_models.append(request.model)
        if not type(self).responses:
            return LLMResponse(message=Message.assistant(""), finish_reason="stop")
        content, tool_calls = type(self).responses.pop(0)
        return LLMResponse(
            message=Message.assistant(content, tool_calls=tool_calls),
            finish_reason="tool_calls" if tool_calls else "stop",
        )

    async def aclose(self) -> None:
        return None


def _cfg(tmp_path: Path) -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="parent-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        ),
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
                model_name="review-model",
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
    monkeypatch.setattr(
        "runtime_assembly.native_runtime.OpenAIResponsesProvider",
        _ScriptedOpenAIProvider,
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
    _ScriptedOpenAIProvider.script(content="review stored")

    cfg = _cfg(tmp_path)
    sink = MemoryEventSink()
    registry = ToolRegistry()
    register_evolution_write_tool_if_enabled(registry, cfg, event_sinks=[sink])
    runtime = NativeRuntime.build(
        cfg,
        tools=registry,
        enabled_tool_names=[],
        event_sinks=[sink],
    )

    result = await runtime.run("please solve this", session_id=session_id)
    await runtime.aclose()

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "parent done"
    assert _ScriptedOpenAIProvider.seen_models == ["parent-model", "review-model", "review-model"]

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
    assert session_state["run_count"] == 1
    assert session_state["last_reviewed_run_id"] == parent_run_id
    assert session_state["last_nutrient_id"] == "nutrient-e2e-1"
    assert session_state["last_review_status"] == "written"

    event_kinds = sink.kinds()
    assert "evolution.review.started" in event_kinds
    assert "evolution.review.completed" in event_kinds
    assert "evolution.nutrient_written" in event_kinds
