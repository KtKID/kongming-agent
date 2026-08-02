"""task_flow workflow strategy 单元测试。

本脚本验证 TaskFlowStrategy 的 payload 解析、默认策略注册、计划产物写入和 session 进度同步。
作用是固定 task_flow 作为通用计划执行 workflow 的运行期合同。
关键执行流程：构造 workflow strategy test manager，调用 run_workflow_payload(mode=task_flow)，断言 workflow artifact、task_flow artifact 和 task_progress.json。
关键函数：_payload 构造最小任务流计划，test_* 覆盖解析和运行产物。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from application.agent_roles import AgentRoleManager
from application.agent_workflows.context import WorkflowRuntime
from application.agent_workflows.strategies.task_flow import TaskFlowStrategy, parse_task_flow_spec
from infrastructure.config.models import Config, ModelSelectionConfig
from tests.support.workflow_strategy_manager import WorkflowStrategyTestManager


def test_parse_task_flow_spec_accepts_llm_plan_payload() -> None:
    """验证 parser，输入为 LLM 计划 payload，输出为规格默认值和节点断言。"""
    spec = parse_task_flow_spec(_payload())

    assert spec.objective == "完成 Web 任务流改造"
    assert spec.title == "任务流改造"
    assert spec.planning["interaction_mode"] == "llm_decide"
    assert spec.planning["choice_policy"] == "ask_when_multiple_viable_paths"
    assert spec.execution["on_unexpected_severe_issue"] == "ask_user"
    assert spec.execution["progress_tool"] == "advance_task_progress"
    assert [node.node_id for node in spec.nodes] == ["step-1", "step-2"]
    assert spec.nodes[1].depends_on == ("step-1",)


def test_task_flow_description_keeps_fixed_progress_tool_out_of_input_payload() -> None:
    """策略详情只展示 LLM 可提交字段，系统固定工具不出现在 execution 样例。"""
    strategy = TaskFlowStrategy(cast(WorkflowRuntime, object()))

    execution = next(field for field in strategy.describe().inputs if field.name == "execution")

    assert execution.example == {"on_unexpected_severe_issue": "ask_user"}
    assert "progress_tool" not in execution.example


@pytest.mark.asyncio
async def test_task_flow_strategy_writes_plan_artifacts_and_task_progress(
    tmp_path: Path,
) -> None:
    """验证 task_flow 运行，输入为两步计划，输出为 artifact 和进度快照断言。"""
    manager = WorkflowStrategyTestManager(
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
    assert task_flow["progress_tool"] == "advance_task_progress"

    plan_path = Path(str(task_flow["plan_path"]))
    progress_path = Path(str(task_flow["progress_path"]))
    assert plan_path.is_file()
    assert progress_path.is_file()

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["objective"] == "完成 Web 任务流改造"
    assert [node["id"] for node in plan["nodes"]] == ["step-1", "step-2"]
    assert plan["nodes"][0]["task_run_id"] == "001-step-1"

    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["status"] == "initial_plan"
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
    assert task_progress["schema_version"] == 2
    assert task_progress["workflow_id"] == result.workflow_id
    assert task_progress["title"] == "任务流改造"
    assert task_progress["control_mode"] == "llm_steps"
    assert task_progress["counts"]["pending"] == 2
    assert [task["status"] for task in task_progress["tasks"]] == ["pending", "pending"]
    assert task_progress["tasks"][1]["depends_on"] == ["step-1"]

    audit_actions = [
        json.loads(line)["action"]
        for line in (result.workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "task_flow.workflow_started" in audit_actions
    assert "task_flow.plan_created" in audit_actions


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        (
            [
                {
                    "id": "step-1",
                    "title": "步骤",
                    "status": "completed",
                }
            ],
            "status is managed internally",
        ),
        (
            [{"id": "step-1", "title": "步骤", "depends_on": ["missing"]}],
            "dependency is missing",
        ),
        (
            [
                {"id": "step-1", "title": "步骤 1", "depends_on": ["step-2"]},
                {"id": "step-2", "title": "步骤 2", "depends_on": ["step-1"]},
            ],
            "contain a cycle",
        ),
    ],
)
def test_parser_rejects_state_injection_and_invalid_dependencies(
    nodes: list[dict[str, object]],
    message: str,
) -> None:
    """Task-flow 计划只能声明任务骨架，状态和依赖闭包由后端校验。"""
    payload = _payload()
    plan = payload["plan"]
    assert isinstance(plan, dict)
    plan["nodes"] = nodes

    with pytest.raises(ValueError, match=message):
        parse_task_flow_spec(payload)


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
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    cfg.session.backend = "file"
    cfg.session.file_store_path = str(tmp_path / "sessions")
    return cfg
