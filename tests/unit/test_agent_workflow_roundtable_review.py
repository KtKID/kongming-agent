"""roundtable_review workflow 集成单元测试。

本脚本验证 Multi-Agent Roundtable Review 策略注册、fake reviewer/arbiter 运行、
ReviewBoard 产物和 run_agent_workflow tool 分流。
作用是用可重复 pytest 覆盖圆桌评审第一版 runtime 集成边界，避免依赖真实模型。
关键执行流程：创建临时源码，构造 roundtable_review payload，用 fake SubAgentManager
返回 claims/comments/report，断言 claims.jsonl、rebuttals.jsonl、final_report.md 和 tool data。
关键函数：_payload 构造 workflow 输入，_FakeRoundtableSubAgentManager 模拟子 agent 输出。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from application.agent_roles import AgentRoleManager
from application.agent_workflows.manager import AgentWorkflowManager
from application.agent_workflows.strategies.roundtable_review.presets import (
    code_review_role_presets,
)
from application.subagents.manager import SubAgentRun
from core import AgentSpec, InMemorySession, Runner, ToolCall
from core.contracts import LLMRequest, LLMResponse, ToolContext
from core.message import Message
from infrastructure.config.models import Config, ModelConfig
from tools import AutoAllowApproval, ToolRegistry, register_agent_role_tool
from tools.agent_workflow_tool import (
    AgentWorkflowHandle,
    build_run_agent_workflow_tool,
)


class _FakeRoundtableSubAgentManager:
    """测试用子 agent manager，按 roundtable_stage 返回结构化输出。"""

    def __init__(self) -> None:
        """初始化 fake manager，输入为空，输出为记录任务的实例。"""
        self.tasks: list[Any] = []

    async def run_task(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        task: Any,
        audit_writer: Any,
    ) -> SubAgentRun:
        """运行 fake 子 agent，输入为任务 metadata，输出为 completed SubAgentRun。"""
        del workflow_id, parent_session_id, audit_writer
        self.tasks.append(task)
        stage = str(task.metadata.get("roundtable_stage"))
        agent = str(task.metadata.get("roundtable_agent"))
        if stage == "independent":
            content = json.dumps(
                {
                    "agent": agent,
                    "findings": [
                        {
                            "severity": "P1",
                            "claim": f"{agent} 发现模块边界风险",
                            "evidence": [
                                {
                                    "type": "code",
                                    "path": "input/source/src/sample.py",
                                    "lines": "1-3",
                                }
                            ],
                            "risk": "后续职责扩展会放大耦合",
                            "suggestion": "拆分门户和内部实现",
                            "confidence": 0.8,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        elif stage == "rebuttal":
            content = json.dumps(
                {
                    "agent": agent,
                    "comments": [
                        {
                            "type": "support",
                            "target_claim_id": "C-001",
                            "comment": f"{agent} 支持 C-001",
                            "evidence": [
                                {
                                    "type": "code",
                                    "path": "input/source/src/sample.py",
                                    "lines": "1-3",
                                }
                            ],
                            "severity_adjustment": None,
                            "confidence": 0.7,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        else:
            content = "\n".join(
                [
                    "# Final Review",
                    "",
                    "## 1. 共识问题",
                    "- C-001 有明确证据。",
                    "",
                    "## 2. 主要分歧",
                    "- 无。",
                    "",
                    "## 3. 高优先级风险",
                    "- P1 模块边界风险。",
                    "",
                    "## 4. 建议修改方案",
                    "- 拆分门户和内部实现。",
                    "",
                    "## 5. 需要人工确认的问题",
                    "- 无。",
                    "",
                    "## 6. 可直接交给开发 Agent 的任务清单",
                    "- 修复 C-001。",
                ]
            )
        return SubAgentRun(
            task=task,
            session_id=f"child-{task.task_id}",
            run_id=f"run-{task.task_id}",
            status="completed",
            content=content,
            error_message=None,
            turn_count=1,
            usage=_usage_for_fake_run(stage),
        )


class _RoleWorkflowLLM:
    """测试用 LLM，按 Runner 轮次依次调用 list/create/run_agent_workflow。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        """初始化 fake LLM，输入为 workflow payload，输出为记录请求的实例。"""
        self._payload = payload
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """完成一次 LLM 请求，输入为历史消息，输出下一步 tool call 或最终回复。"""
        self.calls.append(request)
        completed_tool_names = [
            message.name for message in request.messages if message.role == "tool"
        ]
        if "list_agent_roles" not in completed_tool_names:
            return LLMResponse(
                message=Message.assistant(
                    tool_calls=[
                        ToolCall(
                            call_id="call-list-roles",
                            tool_name="list_agent_roles",
                            arguments={},
                        )
                    ]
                ),
                finish_reason="tool_calls",
            )
        if "create_agent_role" not in completed_tool_names:
            return LLMResponse(
                message=Message.assistant(
                    tool_calls=[
                        ToolCall(
                            call_id="call-create-role",
                            tool_name="create_agent_role",
                            arguments={
                                "id": "risk_skeptic",
                                "title": "风险质询者",
                                "role": "专门寻找方案失败路径和隐藏风险",
                            },
                        )
                    ]
                ),
                finish_reason="tool_calls",
            )
        if "run_agent_workflow" not in completed_tool_names:
            return LLMResponse(
                message=Message.assistant(
                    tool_calls=[
                        ToolCall(
                            call_id="call-run-workflow",
                            tool_name="run_agent_workflow",
                            arguments={
                                "mode": "roundtable_review",
                                "payload": self._payload,
                            },
                        )
                    ]
                ),
                finish_reason="tool_calls",
            )
        return LLMResponse(
            message=Message.assistant("roundtable workflow completed"),
            finish_reason="stop",
        )


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 file session 配置。"""
    cfg = Config(
        model=ModelConfig(
            name="fake-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        )
    )
    cfg.session.file_store_path = str(tmp_path / "sessions")
    return cfg


def _payload() -> dict[str, Any]:
    """构造 roundtable_review payload，输入为空，输出为 participants.select JSON。"""
    return {
        "topic": "Session 模块设计是否合理",
        "participants": {
            "select": [
                "architecture_reviewer",
                "code_quality_reviewer",
                "test_reviewer",
                "performance_reviewer",
                "safety_stability_reviewer",
            ]
        },
        "input_source": {
            "root_dir": ".",
            "paths": ["src"],
            "include": [],
            "exclude": ["__pycache__/**"],
            "max_files": 10,
            "max_bytes_per_file": 80000,
        },
        "limits": {
            "total_child_token_budget": 50000,
            "discussion_rounds": 2,
            "max_discussion_rounds": 6,
            "max_concurrency": 5,
            "agent_timeout_seconds": 30,
        },
        "audit_tags": ["unit", "roundtable"],
    }


def _write_sample_source(tmp_path: Path) -> None:
    """写入临时源码，输入为目录，输出为 sample.py 文件。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "sample.py").write_text(
        "class SessionManager:\n    def run(self):\n        return 'ok'\n",
        encoding="utf-8",
    )


def _usage_for_fake_run(stage: str) -> dict[str, int]:
    """构造 fake 子 agent usage，输入为 roundtable stage，输出含动态字段的 usage。"""
    if stage == "independent":
        return {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 3,
            "cache_creation_tokens": 5,
            "provider_extra_tokens": 7,
        }
    if stage == "rebuttal":
        return {
            "input_tokens": 80,
            "output_tokens": 10,
            "cache_read_tokens": 2,
            "cache_creation_tokens": 1,
            "provider_extra_tokens": 4,
        }
    return {
        "input_tokens": 200,
        "output_tokens": 50,
        "cache_read_tokens": 6,
        "cache_creation_tokens": 2,
        "provider_extra_tokens": 9,
    }


def _role_manager(tmp_path: Path) -> AgentRoleManager:
    """构造带 code review 内置角色的 manager，输入为 tmp_path，输出角色门户。"""
    return AgentRoleManager(
        role_dir=tmp_path / "roles",
        builtin_roles=code_review_role_presets(),
    )


@pytest.mark.asyncio
async def test_roundtable_review_strategy_writes_review_board(tmp_path: Path) -> None:
    """验证 roundtable_review 产物，输入为 fake 子 agent，输出为白板断言。"""
    _write_sample_source(tmp_path)
    fake = _FakeRoundtableSubAgentManager()
    manager = AgentWorkflowManager(
        subagents=fake,  # type: ignore[arg-type]
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=_role_manager(tmp_path),
    )

    result = await manager.run_workflow_payload(
        mode="roundtable_review",
        parent_session_id="parent-session",
        payload=_payload(),
    )

    assert result.completed is True
    assert result.mode == "roundtable_review"
    assert (
        len([task for task in fake.tasks if task.metadata["roundtable_stage"] == "independent"])
        == 5
    )
    assert all(task.metadata["max_turns"] == 8 for task in fake.tasks)
    board = result.workflow_dir / "review_board"
    claims = (board / "claims.jsonl").read_text(encoding="utf-8").strip().splitlines()
    rebuttals = (board / "rebuttals.jsonl").read_text(encoding="utf-8").strip().splitlines()
    final_report = (board / "final_report.md").read_text(encoding="utf-8")
    assert len(claims) == 5
    assert len(rebuttals) == 5
    assert "Final Review" in final_report
    assert (board / "context.md").exists()
    assert (board / "sources.md").exists()
    assert (board / "consensus.md").exists()
    roles = json.loads((result.workflow_dir / "roles.json").read_text(encoding="utf-8"))
    assert roles["source"] == "agent_role_manager"
    assert [role["id"] for role in roles["roles"]] == _payload()["participants"]["select"]
    assert result.data is not None
    assert result.data["roundtable_review"]["claim_count"] == 5  # type: ignore[index]
    assert Path(result.data["roundtable_review"]["role_snapshot_path"]).exists()  # type: ignore[index]
    roundtable_data = result.data["roundtable_review"]  # type: ignore[index]
    assert roundtable_data["estimated_child_output_tokens"] > 0
    assert len(roundtable_data["child_agent_usages"]) == 11
    assert roundtable_data["child_agent_usage_totals"] == {
        "input_tokens": 1100,
        "output_tokens": 200,
        "cache_read_tokens": 31,
        "cache_creation_tokens": 32,
        "provider_extra_tokens": 64,
    }
    result_payload = json.loads((result.workflow_dir / "result.json").read_text(encoding="utf-8"))
    assert result_payload["roundtable_review"]["child_agent_usages"][0]["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 3,
        "cache_creation_tokens": 5,
        "provider_extra_tokens": 7,
    }
    assert (
        result_payload["roundtable_review"]["child_agent_usage_totals"]
        == roundtable_data["child_agent_usage_totals"]
    )


def test_agent_workflow_manager_registers_roundtable_review(tmp_path: Path) -> None:
    """验证默认策略目录包含 roundtable_review，输入为 manager，输出为 catalog 断言。"""
    manager = AgentWorkflowManager(
        subagents=object(),  # type: ignore[arg-type]
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=_role_manager(tmp_path),
    )

    catalog = manager.list_workflow_strategies()
    assert [entry.mode for entry in catalog] == [
        "deep_research",
        "map_reduce",
        "parallel",
        "roundtable_review",
    ]
    description = manager.describe_workflow_strategy("roundtable_review")
    assert description.runnable is True
    assert description.inputs[0].name == "topic"


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_dispatches_roundtable_review(tmp_path: Path) -> None:
    """验证 tool 入口分流 roundtable_review，输入为 payload，输出为 data 路径断言。"""
    _write_sample_source(tmp_path)
    manager = AgentWorkflowManager(
        subagents=_FakeRoundtableSubAgentManager(),  # type: ignore[arg-type]
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=_role_manager(tmp_path),
    )
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    result = await tool.execute(
        {"mode": "roundtable_review", "payload": _payload()},
        ToolContext(run_id="r", session_id="parent-session", turn=1, call_id="c"),
    )

    assert result.ok is True
    assert "roundtable_final_report" in result.content
    assert result.data is not None
    assert result.data["mode"] == "roundtable_review"
    board = result.data["roundtable_review"]["review_board"]  # type: ignore[index]
    assert Path(board["final_report_path"]).exists()


@pytest.mark.asyncio
async def test_runner_fake_llm_creates_role_then_runs_roundtable(tmp_path: Path) -> None:
    """验证 Runner 工具链路，输入为 fake LLM tool calls，输出动态角色 workflow。"""
    _write_sample_source(tmp_path)
    role_manager = AgentRoleManager(role_dir=tmp_path / "roles")
    manager = AgentWorkflowManager(
        subagents=_FakeRoundtableSubAgentManager(),  # type: ignore[arg-type]
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=role_manager,
    )
    payload = _payload()
    payload["participants"] = {"select": ["risk_skeptic"]}
    llm = _RoleWorkflowLLM(payload)
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    registry = ToolRegistry()
    register_agent_role_tool(registry, role_manager)
    registry.register(build_run_agent_workflow_tool(handle))
    session = InMemorySession("parent-session")

    result = await Runner().run(
        "请用圆桌讨论审查这个话题",
        session=session,
        agent_spec=AgentSpec(
            name="parent",
            instructions="use tools",
            default_model="fake-model",
            max_turns=6,
        ),
        llm=llm,
        tools=registry,
        approval=AutoAllowApproval(),
        enabled_tools=registry.all_tools(),
    )

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "roundtable workflow completed"
    history = await session.history()
    tool_messages = [message for message in history if message.role == "tool"]
    assert [message.name for message in tool_messages] == [
        "list_agent_roles",
        "create_agent_role",
        "run_agent_workflow",
    ]
    created = tool_messages[1].metadata["data"]
    assert created["current_roundtable_agents"] == [created["role"]]
    workflow_data = tool_messages[2].metadata["data"]
    workflow_dir = Path(workflow_data["workflow_dir"])
    roles = json.loads((workflow_dir / "roles.json").read_text(encoding="utf-8"))
    assert roles["roles"] == [
        {
            "id": "risk_skeptic",
            "title": "风险质询者",
            "role": "专门寻找方案失败路径和隐藏风险",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_patch", "message"),
    [
        ({"reviewers": ["architecture_reviewer"]}, "reviewers is removed"),
        ({"participants": {"create": []}}, "unsupported participants field: create"),
        ({"participants": {"preset": "code_review"}}, "unsupported participants field: preset"),
        (
            {"participants": {"select": ["architecture_reviewer"], "extra": []}},
            "unsupported participants field: extra",
        ),
        ({"participants": {"select": []}}, "participants.select is required"),
        ({"participants": {"select": ["missing_role"]}}, "unknown role id: missing_role"),
    ],
)
async def test_roundtable_review_rejects_invalid_participants(
    tmp_path: Path,
    payload_patch: dict[str, Any],
    message: str,
) -> None:
    """验证 participants 旧字段拒绝矩阵，输入为坏 payload，输出参数错误。"""
    _write_sample_source(tmp_path)
    payload = _payload()
    payload.update(payload_patch)
    manager = AgentWorkflowManager(
        subagents=_FakeRoundtableSubAgentManager(),  # type: ignore[arg-type]
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=_role_manager(tmp_path),
    )

    with pytest.raises(ValueError, match=message):
        await manager.run_workflow_payload(
            mode="roundtable_review",
            parent_session_id="parent-session",
            payload=payload,
        )
