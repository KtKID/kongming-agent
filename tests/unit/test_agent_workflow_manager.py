from __future__ import annotations

import hashlib
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
from executors.agent_runtime.agent_workflow_manager import (
    AgentWorkflowManager,
    SubAgentReportProjection,
)
from executors.agent_runtime.native_runtime import NativeRuntime
from executors.agent_runtime.subagent_manager import SubAgentManager, SubAgentTask
from tools import AutoAllowApproval, ToolRegistry
from tools.agent_workflow_tool import AgentWorkflowHandle, build_agent_workflow_tool
from tools.file_tools import build_file_tools


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
                                    {
                                        "task_name": "alpha",
                                        "prompt": "alpha child task",
                                        "permission": {"mode": "scoped_workdir"},
                                    },
                                    {
                                        "task_name": "beta",
                                        "prompt": "beta child task",
                                        "permission": {"mode": "scoped_workdir"},
                                    },
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


class _ToolCallingLLM:
    def __init__(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        final_content: str = "tool result observed",
    ) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.final_content = final_content
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        has_tool_result = any(message.role == "tool" for message in request.messages)
        if has_tool_result:
            return LLMResponse(
                message=Message.assistant(self.final_content),
                finish_reason="stop",
            )
        return LLMResponse(
            message=Message.assistant(
                tool_calls=[
                    ToolCall(
                        call_id="call-child-tool",
                        tool_name=self.tool_name,
                        arguments=self.arguments,
                    )
                ]
            ),
            finish_reason="tool_calls",
        )


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


def _runtime(
    tmp_path: Path,
    llm: Any,
    *,
    tools: ToolRegistry | None = None,
) -> tuple[Config, NativeRuntime]:
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
        tools=tools or ToolRegistry(),
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


def _audit_records(workflow_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]


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
    assert result.report_index_path.is_file()
    workflow = json.loads((result.workflow_dir / "workflow.json").read_text(encoding="utf-8"))
    assert workflow["status"] == "completed"
    assert len(workflow["assigned_agents"]) == 2

    audit_records = [
        json.loads(line)
        for line in (result.workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    audit_actions = [record["action"] for record in audit_records]
    assert audit_actions.count("workflow_started") == 1
    assert audit_actions.count("agent_assigned") == 2
    assert audit_actions.count("agent_completed") == 2
    assert audit_actions.count("subagent_reported") == 2
    assert audit_actions.count("workflow_completed") == 1

    report_index = json.loads(result.report_index_path.read_text(encoding="utf-8"))
    assert report_index["status"] == "completed"
    assert [report["task_id"] for report in report_index["reports"]] == ["alpha", "beta"]
    assert [report["display_order"] for report in report_index["reports"]] == [1, 2]
    assert "content" not in report_index["reports"][0]

    workflow_result = json.loads((result.workflow_dir / "result.json").read_text(encoding="utf-8"))
    assert workflow_result["report_index_path"] == str(result.report_index_path)
    assert [report["task_id"] for report in workflow_result["reports"]] == ["alpha", "beta"]

    user_messages = [
        "\n".join(message.content or "" for message in call.messages if message.role == "user")
        for call in llm.calls
    ]
    assert any("alpha task" in text for text in user_messages)
    assert any("beta task" in text for text in user_messages)
    assert all("alpha task" not in text or "beta task" not in text for text in user_messages)

    for index, run in enumerate(result.runs):
        session_dir = Path(cfg.session.file_store_path) / run.session_id
        assert session_dir.is_dir()
        assert (session_dir / f"{run.session_id}.jsonl").is_file()
        report_path = Path(result.reports[index].report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        expected_digest = f"sha256:{hashlib.sha256(report['content'].encode()).hexdigest()}"
        assert report["task_id"] == run.task.task_id
        assert report["status"] == "completed"
        assert report["content_digest"] == expected_digest
        assert report["summary"]
        reported = next(
            record["payload"]
            for record in audit_records
            if record["action"] == "subagent_reported"
            and record["payload"]["task_id"] == run.task.task_id
        )
        assert reported["report_path"] == str(report_path)
        assert reported["content_digest"] == report["content_digest"]


@pytest.mark.asyncio
async def test_parallel_workflow_reports_failed_child_task(tmp_path: Path) -> None:
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
                SubAgentTask(
                    task_id="bad-tool",
                    task_name="bad tool",
                    prompt="bad task",
                    tool_names=("missing_tool",),
                )
            ],
        )
    finally:
        await runtime.aclose()

    assert result.completed is False
    assert result.runs[0].status == "failed"
    assert result.reports[0].status == "failed"
    assert result.report_index_path.is_file()

    audit_actions = [
        json.loads(line)["action"]
        for line in (result.workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert audit_actions.count("agent_failed") == 1
    assert audit_actions.count("subagent_reported") == 1
    assert audit_actions.count("workflow_completed") == 1

    report_path = Path(result.reports[0].report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert "missing_tool" in report["error_message"]
    assert report["content_digest"].startswith("sha256:")

    workflow_result = json.loads((result.workflow_dir / "result.json").read_text(encoding="utf-8"))
    assert workflow_result["completed"] is False
    assert workflow_result["reports"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_parallel_workflow_uses_unique_task_run_paths_for_slug_collisions(
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
                SubAgentTask(task_id="a/b", task_name="alpha", prompt="alpha task"),
                SubAgentTask(task_id="a?b", task_name="beta", prompt="beta task"),
            ],
        )
    finally:
        await runtime.aclose()

    assert result.completed is True
    assert len({run.session_id for run in result.runs}) == 2
    assert {Path(report.report_path).name for report in result.reports} == {
        "001-a-b.json",
        "002-a-b.json",
    }
    assert (result.workflow_dir / "agents" / "001-a-b" / "result.json").is_file()
    assert (result.workflow_dir / "agents" / "002-a-b" / "result.json").is_file()

    report_index = json.loads(result.report_index_path.read_text(encoding="utf-8"))
    assert [report["task_id"] for report in report_index["reports"]] == ["a/b", "a?b"]
    assert len({report["report_path"] for report in report_index["reports"]}) == 2


@pytest.mark.asyncio
async def test_scoped_parallel_workflow_writes_subagent_creation_and_workdir_file(
    tmp_path: Path,
) -> None:
    llm = _ToolCallingLLM(
        tool_name="write_file",
        arguments={"path": "result.txt", "content": "scoped ok"},
    )
    cfg, runtime = _runtime(tmp_path, llm, tools=ToolRegistry(build_file_tools()))
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
        )
        result = await manager.run_workflow_specs(
            mode="parallel",
            parent_session_id="parent-session",
            task_specs=[
                {
                    "task_name": "write ok",
                    "prompt": "write result.txt",
                    "tool_names": ["write_file"],
                    "skill_names": ["writer"],
                    "permission": {"mode": "scoped_workdir"},
                }
            ],
        )
    finally:
        await runtime.aclose()

    assert result.completed is True
    task_run_dir = result.workflow_dir / "agents" / "001-agent-1"
    working_dir = task_run_dir / "work"
    assert (working_dir / "result.txt").read_text(encoding="utf-8") == "scoped ok"
    creation = json.loads((task_run_dir / "subagent.json").read_text(encoding="utf-8"))
    assert creation["task_run_id"] == "001-agent-1"
    assert creation["permission"] == {"mode": "scoped_workdir"}
    assert creation["grant"]["allowed_tools"] == ["write_file"]
    assert creation["grant"]["allowed_skills"] == ["writer"]
    assert creation["working_dir"] == str(working_dir)
    assert Path(result.reports[0].working_dir or "") == working_dir

    records = _audit_records(result.workflow_dir)
    actions = [record["action"] for record in records]
    assert "subagent_created" in actions
    assert "subagent_grant_bound" in actions
    approval_payload = next(
        record["payload"] for record in records if record["action"] == "subagent_approval_decided"
    )
    assert approval_payload["decision"] == "approved"
    assert approval_payload["decision_source"] == "grant_allow"
    assert approval_payload["resolved_path"] == str(working_dir / "result.txt")


@pytest.mark.asyncio
async def test_scoped_parallel_workflow_rejects_outside_write_without_side_effect(
    tmp_path: Path,
) -> None:
    llm = _ToolCallingLLM(
        tool_name="write_file",
        arguments={"path": "../outside.txt", "content": "bad"},
        final_content="write rejected by scoped permission",
    )
    cfg, runtime = _runtime(tmp_path, llm, tools=ToolRegistry(build_file_tools()))
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
        )
        result = await manager.run_workflow_specs(
            mode="parallel",
            parent_session_id="parent-session",
            task_specs=[
                {
                    "task_name": "write denied",
                    "prompt": "try outside write",
                    "tool_names": ["write_file"],
                    "permission": {"mode": "scoped_workdir"},
                }
            ],
        )
    finally:
        await runtime.aclose()

    assert result.completed is True
    task_run_dir = result.workflow_dir / "agents" / "001-agent-1"
    assert not (task_run_dir / "outside.txt").exists()
    assert result.reports[0].summary == "write rejected by scoped permission"
    approval_payload = next(
        record["payload"]
        for record in _audit_records(result.workflow_dir)
        if record["action"] == "subagent_approval_decided"
    )
    assert approval_payload["decision"] == "rejected"
    assert approval_payload["decision_source"] == "scope_deny"
    assert approval_payload["target_path"] == "../outside.txt"


@pytest.mark.asyncio
async def test_scoped_parallel_workflow_audits_hallucinated_not_registered_tool(
    tmp_path: Path,
) -> None:
    llm = _ToolCallingLLM(
        tool_name="missing_tool",
        arguments={"path": "../outside.txt", "x": 1},
        final_content="missing tool rejected",
    )
    cfg, runtime = _runtime(tmp_path, llm, tools=ToolRegistry(build_file_tools()))
    try:
        manager = AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=cfg,
            workspace_root=tmp_path,
        )
        result = await manager.run_workflow_specs(
            mode="parallel",
            parent_session_id="parent-session",
            task_specs=[
                {
                    "task_name": "hallucinated tool",
                    "prompt": "call a missing tool",
                    "tool_names": ["write_file"],
                    "permission": {"mode": "scoped_workdir"},
                }
            ],
        )
    finally:
        await runtime.aclose()

    assert result.completed is True
    approval_payload = next(
        record["payload"]
        for record in _audit_records(result.workflow_dir)
        if record["action"] == "subagent_approval_decided"
    )
    assert approval_payload["decision"] == "rejected"
    assert approval_payload["decision_source"] == "not_registered"
    assert approval_payload["tool_name"] == "missing_tool"
    assert approval_payload["raw_args"] == {"path": "../outside.txt", "x": 1}


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
                report_index_path = Path("/tmp/wf-test/reports/index.json")
                completed = True
                reports = (
                    SubAgentReportProjection(
                        display_order=1,
                        task_id="agent-1",
                        task_name="calc",
                        status="completed",
                        summary="calculation complete",
                        error_message=None,
                        report_path="/tmp/wf-test/reports/agent-1.json",
                        working_dir="/tmp/wf-test/agents/agent-1",
                        session_id="subagent-test",
                        run_id="run-subagent-test-1",
                        reported_at="2026-06-06T00:00:00+00:00",
                    ),
                    SubAgentReportProjection(
                        display_order=2,
                        task_id="agent-2",
                        task_name="empty",
                        status="completed",
                        summary="",
                        error_message=None,
                        report_path="/tmp/wf-test/reports/agent-2.json",
                        working_dir="/tmp/wf-test/agents/agent-2",
                        session_id="subagent-empty",
                        run_id="run-subagent-empty-1",
                        reported_at="2026-06-06T00:00:01+00:00",
                    ),
                )
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
                    "skill_names": ["review-docs"],
                    "permission": {"mode": "scoped_workdir"},
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
            "skill_names": ["review-docs"],
            "permission": {"mode": "scoped_workdir"},
        }
    ]
    assert "wf-test" in content
    assert "calculation complete" in content
    assert content.count("summary:") == 2
    assert "error_message: None" in content
    assert "working_dir: /tmp/wf-test/agents/agent-1" in content
    assert "synthesize the final answer" in content
    assert data is not None
    assert data["workflow_id"] == "wf-test"
    assert data["report_index_path"] == "/tmp/wf-test/reports/index.json"
    assert data["reports"][0]["summary"] == "calculation complete"
    assert data["reports"][0]["error_message"] is None


@pytest.mark.asyncio
async def test_agent_workflow_tool_requires_permission() -> None:
    handle = AgentWorkflowHandle()
    handle.bind(object())
    tool = build_agent_workflow_tool(handle)

    with pytest.raises(ValueError, match="permission must be an object"):
        await tool._run(
            {
                "tasks": [
                    {
                        "task_name": "calc",
                        "prompt": "calculate",
                    }
                ]
            },
            ToolContext(run_id="r", session_id="parent-session", turn=1, call_id="c"),
        )


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
    tool_messages = [
        message
        for request in llm.calls
        for message in request.messages
        if message.role == "tool" and message.name == "run_parallel_subagents"
    ]
    assert len(tool_messages) == 1
    tool_content = tool_messages[0].content or ""
    assert "report_index:" in tool_content
    assert "summary: alpha child result" in tool_content
    assert "summary: beta child result" in tool_content
    assert "synthesize the final answer" in tool_content
    workflow_roots = list(
        (Path(cfg.session.file_store_path) / "parent-session").glob("agent-workflows/wf-*")
    )
    assert len(workflow_roots) == 1
    workflow_result = json.loads((workflow_roots[0] / "result.json").read_text(encoding="utf-8"))
    assert workflow_result["completed"] is True
    assert {report["summary"] for report in workflow_result["reports"]} == {
        "alpha child result",
        "beta child result",
    }
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
