from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli.main import _apply_model_preset_or_exit
from config_loader.models import Config, LLMPresetConfig, ModelConfig, WebConfig
from context import SessionBootstrap, build_session
from core.agent_spec import AgentSpec
from core.contracts import LLMRequest, LLMResponse, ToolContext
from core.message import Message, ToolCall
from core.runner import Runner
from executors.agent_runtime.agent_workflow_manager import AgentWorkflowManager
from executors.agent_runtime.native_runtime import NativeRuntime
from executors.agent_runtime.subagent_manager import SubAgentManager, SubAgentTask
from tools import AutoAllowApproval, ToolRegistry
from tools.agent_workflow_tool import AgentWorkflowHandle, build_agent_workflow_tool


class _EchoLLM:
    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        user_text = "\n".join(
            message.content or "" for message in request.messages if message.role == "user"
        )
        if "7 + 5" in user_text:
            content = "12"
        elif "alpha" in user_text:
            content = "alpha done"
        elif "beta" in user_text:
            content = "beta done"
        else:
            content = "done"
        return LLMResponse(message=Message.assistant(content), finish_reason="stop")


class _WorkflowLLM:
    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        tool_names = {tool.name for tool in request.tools}
        has_workflow_result = any(
            message.role == "tool" and message.name == "run_parallel_subagents"
            for message in request.messages
        )
        if "run_parallel_subagents" in tool_names and not has_workflow_result:
            return LLMResponse(
                message=Message.assistant(
                    tool_calls=[
                        ToolCall(
                            call_id="call-workflow",
                            tool_name="run_parallel_subagents",
                            arguments={
                                "tasks": [
                                    {"task_name": "alpha", "prompt": "alpha child task"},
                                    {"task_name": "beta", "prompt": "beta child task"},
                                ]
                            },
                        )
                    ]
                ),
                finish_reason="tool_calls",
            )
        user_text = "\n".join(
            message.content or "" for message in request.messages if message.role == "user"
        )
        if "alpha child task" in user_text:
            return LLMResponse(
                message=Message.assistant("alpha child result"), finish_reason="stop"
            )
        if "beta child task" in user_text:
            return LLMResponse(message=Message.assistant("beta child result"), finish_reason="stop")
        return LLMResponse(message=Message.assistant("parent summary"), finish_reason="stop")


def _config(tmp_path: Path) -> Config:
    cfg = Config(
        model=ModelConfig(
            name="fake-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        )
    )
    cfg.session.backend = "file"
    cfg.session.file_store_path = str(tmp_path / "sessions")
    cfg.approval.mode = "auto_allow"
    return cfg


def _runtime(tmp_path: Path, llm: _EchoLLM) -> tuple[Config, NativeRuntime]:
    cfg = _config(tmp_path)
    bootstrap = SessionBootstrap(
        agent_name="test-agent",
        model_name=cfg.model.name,
        instruction_sources=[],
        instruction_text_hash="test",
        created_at=1.0,
        cwd=str(tmp_path),
    )

    def session_factory(sid: str):  # type: ignore[no-untyped-def]
        return build_session(cfg, sid, bootstrap=bootstrap)

    runtime = NativeRuntime(
        config=cfg,
        runner=Runner(),
        llm=llm,
        tools=ToolRegistry(),
        enabled_tool_names=[],
        approval=AutoAllowApproval(),
        session_factory=session_factory,
        event_sinks=[],
        agent_spec=AgentSpec(
            name="parent",
            instructions="parent instructions",
            default_model=cfg.model.name,
        ),
    )
    return cfg, runtime


@pytest.mark.asyncio
async def test_parallel_workflow_writes_audit_and_keeps_child_contexts_isolated(
    tmp_path: Path,
) -> None:
    llm = _EchoLLM()
    cfg, runtime = _runtime(tmp_path, llm)
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
        )
        result = await manager.run_parallel(
            parent_session_id="parent-session",
            tasks=[
                SubAgentTask(task_id="alpha", task_name="alpha", prompt="alpha task"),
                SubAgentTask(task_id="beta", task_name="beta", prompt="beta task"),
            ],
        )
    finally:
        await runtime.aclose()

    assert result.completed is True
    assert result.workflow_dir.is_dir()
    workflow = json.loads((result.workflow_dir / "workflow.json").read_text(encoding="utf-8"))
    assert workflow["status"] == "completed"
    assert len(workflow["assigned_agents"]) == 2

    audit_actions = [
        json.loads(line)["action"]
        for line in (result.workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert audit_actions.count("workflow_started") == 1
    assert audit_actions.count("agent_assigned") == 2
    assert audit_actions.count("agent_completed") == 2
    assert audit_actions.count("workflow_completed") == 1

    user_messages = [
        "\n".join(message.content or "" for message in call.messages if message.role == "user")
        for call in llm.calls
    ]
    assert any("alpha task" in text for text in user_messages)
    assert any("beta task" in text for text in user_messages)
    assert all("alpha task" not in text or "beta task" not in text for text in user_messages)

    for run in result.runs:
        session_dir = Path(cfg.session.file_store_path) / run.session_id
        assert session_dir.is_dir()
        assert (session_dir / f"{run.session_id}.jsonl").is_file()


@pytest.mark.asyncio
async def test_agent_workflow_tool_passes_task_specs_to_bound_manager() -> None:
    class _Manager:
        def __init__(self) -> None:
            self.mode = ""
            self.parent_session_id = ""
            self.task_specs: list[dict[str, object]] = []

        async def run_workflow_specs(
            self,
            *,
            mode: str,
            parent_session_id: str,
            task_specs: list[dict[str, object]],
        ) -> Any:
            self.mode = mode
            self.parent_session_id = parent_session_id
            self.task_specs = task_specs

            class _Result:
                workflow_id = "wf-test"
                mode = "parallel"
                workflow_dir = Path("/tmp/wf-test")
                completed = True
                runs: list[Any] = []

            return _Result()

    handle = AgentWorkflowHandle()
    manager = _Manager()
    handle.bind(manager)
    tool = build_agent_workflow_tool(handle)
    content, data = await tool._run(
        {
            "tasks": [
                {
                    "task_name": "calc",
                    "prompt": "calculate",
                    "context": "only this",
                    "tool_names": ["write_file"],
                }
            ]
        },
        ToolContext(run_id="r", session_id="parent-session", turn=1, call_id="c"),
    )

    assert manager.mode == "parallel"
    assert manager.parent_session_id == "parent-session"
    assert manager.task_specs == [
        {
            "task_name": "calc",
            "prompt": "calculate",
            "context": "only this",
            "tool_names": ["write_file"],
        }
    ]
    assert "wf-test" in content
    assert data is not None
    assert data["workflow_id"] == "wf-test"


@pytest.mark.asyncio
async def test_parent_agent_can_create_subagents_through_registered_tool(tmp_path: Path) -> None:
    llm = _WorkflowLLM()
    cfg = _config(tmp_path)
    bootstrap = SessionBootstrap(
        agent_name="test-agent",
        model_name=cfg.model.name,
        instruction_sources=[],
        instruction_text_hash="test",
        created_at=1.0,
        cwd=str(tmp_path),
    )

    def session_factory(sid: str):  # type: ignore[no-untyped-def]
        return build_session(cfg, sid, bootstrap=bootstrap)

    handle = AgentWorkflowHandle()
    registry = ToolRegistry([build_agent_workflow_tool(handle)])
    runtime = NativeRuntime(
        config=cfg,
        runner=Runner(),
        llm=llm,
        tools=registry,
        enabled_tool_names=["run_parallel_subagents"],
        approval=AutoAllowApproval(),
        session_factory=session_factory,
        event_sinks=[],
        agent_spec=AgentSpec(
            name="parent",
            instructions="parent instructions",
            default_model=cfg.model.name,
            tool_names=("run_parallel_subagents",),
            max_turns=5,
        ),
    )
    manager = AgentWorkflowManager(
        subagents=SubAgentManager(runtime),
        config=cfg,
        workspace_root=tmp_path,
    )
    handle.bind(manager)
    try:
        result = await runtime.run("delegate work", session_id="parent-session")
    finally:
        await runtime.aclose()

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "parent summary"
    workflow_roots = list(
        (Path(cfg.session.file_store_path) / "parent-session").glob("agent-workflows/wf-*")
    )
    assert len(workflow_roots) == 1
    workflow_result = json.loads((workflow_roots[0] / "result.json").read_text(encoding="utf-8"))
    assert workflow_result["completed"] is True
    assert {run["content"] for run in workflow_result["runs"]} == {
        "alpha child result",
        "beta child result",
    }


def test_apply_model_preset_overrides_cli_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")
    cfg = Config(
        model=ModelConfig(
            name="local",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        ),
        web=WebConfig(
            llm_presets=[
                LLMPresetConfig(
                    id="minimax-m3",
                    display_name="MiniMax M3",
                    base_url="https://api.minimaxi.com/anthropic",
                    model="MiniMax-M3",
                    api_key_env="MINIMAX_API_KEY",
                    reasoning_effort="high",
                )
            ]
        ),
    )

    updated = _apply_model_preset_or_exit(cfg, "minimax-m3")

    assert updated.model.name == "MiniMax-M3"
    assert updated.model.base_url == "https://api.minimaxi.com/anthropic"
    assert updated.model.api_key == "sk-test"
    assert updated.model.reasoning_effort == "high"
    assert updated.model.effective_provider == "anthropic"
