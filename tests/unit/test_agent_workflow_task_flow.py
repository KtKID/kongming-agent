"""task_flow workflow strategy 单元测试。

本脚本验证 TaskFlowStrategy 的 payload 解析、默认策略注册、计划产物写入和 session 进度同步。
作用是固定 task_flow 作为通用计划执行 workflow 的运行期合同。
关键执行流程：构造 AgentWorkflowManager，调用 run_workflow_payload(mode=task_flow)，断言 workflow artifact、task_flow artifact 和 task_progress.json。
关键函数：_payload 构造最小任务流计划，test_* 覆盖解析和运行产物。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from application.agent_roles import AgentRoleManager
from application.agent_workflows.manager import AgentWorkflowManager
from application.agent_workflows.strategies.task_flow import parse_task_flow_spec
from infrastructure.config.models import Config, ModelConfig


def test_parse_task_flow_spec_accepts_llm_plan_payload() -> None:
    """验证 parser，输入为 LLM 计划 payload，输出为规格默认值和节点断言。"""
    spec = parse_task_flow_spec(_payload())

    assert spec.objective == "完成 Web 任务流改造"
    assert spec.title == "任务流改造"
    assert spec.planning["interaction_mode"] == "llm_decide"
    assert spec.planning["choice_policy"] == "ask_when_multiple_viable_paths"
    assert spec.execution["on_unexpected_severe_issue"] == "ask_user"
    assert spec.execution["progress_tool"] == "update_task_progress"
    assert [node.node_id for node in spec.nodes] == ["step-1", "step-2"]
    assert spec.nodes[1].depends_on == ("step-1",)


@pytest.mark.asyncio
async def test_task_flow_strategy_writes_plan_artifacts_and_task_progress(
    tmp_path: Path,
) -> None:
    """验证 task_flow 运行，输入为两步计划，输出为 artifact 和进度快照断言。"""
    manager = AgentWorkflowManager(
        subagents=object(),  # type: ignore[arg-type]
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )

    result = await manager.run_workflow_payload(
        mode="task_flow",
        parent_session_id="parent-session",
        payload=_payload(),
    )

    assert result.completed is True
    assert result.data is not None
    task_flow = result.data["task_flow"]
    assert isinstance(task_flow, dict)
    assert task_flow["node_count"] == 2
    assert task_flow["progress_tool"] == "update_task_progress"

    plan_path = Path(str(task_flow["plan_path"]))
    progress_path = Path(str(task_flow["progress_path"]))
    assert plan_path.is_file()
    assert progress_path.is_file()

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["objective"] == "完成 Web 任务流改造"
    assert [node["id"] for node in plan["nodes"]] == ["step-1", "step-2"]
    assert plan["nodes"][0]["task_run_id"] == "001-step-1"

    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["status"] == "plan_created"
    assert progress["counts"] == {
        "pending": 2,
        "in_progress": 0,
        "completed": 0,
        "total": 2,
    }

    workflow = json.loads((result.workflow_dir / "workflow.json").read_text(encoding="utf-8"))
    assert workflow["status"] == "plan_created"
    assert [item["task_id"] for item in workflow["assigned_agents"]] == [
        "step-1",
        "step-2",
    ]

    task_progress_path = tmp_path / "sessions" / "parent-session" / "task_progress.json"
    task_progress = json.loads(task_progress_path.read_text(encoding="utf-8"))
    assert task_progress["source"] == "workflow"
    assert task_progress["counts"]["pending"] == 2
    assert [task["source_status"] for task in task_progress["tasks"]] == [
        "assigned",
        "assigned",
    ]

    audit_actions = [
        json.loads(line)["action"]
        for line in (result.workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "task_flow.workflow_started" in audit_actions
    assert "task_flow.plan_created" in audit_actions


def _payload() -> dict[str, object]:
    """构造 task_flow 测试 payload，输入为空，输出为两步计划字典。"""
    return {
        "objective": "完成 Web 任务流改造",
        "planning": {
            "interaction_mode": "llm_decide",
            "choice_policy": "ask_when_multiple_viable_paths",
        },
        "plan": {
            "title": "任务流改造",
            "nodes": [
                {
                    "id": "step-1",
                    "title": "梳理入口",
                    "description": "确认 workflow 策略和工具入口。",
                },
                {
                    "id": "step-2",
                    "title": "接入 task_flow",
                    "description": "注册策略并写入 prompt 说明。",
                    "depends_on": ["step-1"],
                },
            ],
        },
        "execution": {"on_unexpected_severe_issue": "ask_user"},
        "audit_tags": ["task_flow"],
    }


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 file session 配置。"""
    cfg = Config(
        model=ModelConfig(
            name="fake-model",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        )
    )
    cfg.session.backend = "file"
    cfg.session.file_store_path = str(tmp_path / "sessions")
    return cfg
