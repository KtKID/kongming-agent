"""Task Flow deterministic e2e 测试。

本脚本验证 run_agent_workflow tool、AgentWorkflowManager、TaskFlowStrategy 和
update_task_progress 工具的完整离线链路。
作用是固定 task_flow 的中等复杂度合同：先计划，再由 LLM 推进进度。
关键流程：从 tool call 创建四步任务计划，读取 workflow/task_flow/session 产物，
再模拟主 LLM 逐步调用 update_task_progress 完成任务。
关键函数覆盖计划创建和执行进度更新主链路。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from application.agent_roles import AgentRoleManager
from application.agent_workflows.manager import AgentWorkflowManager
from core.contracts import ToolContext
from infrastructure.config.models import Config, ModelConfig
from tools.agent_workflow_tool import AgentWorkflowHandle, build_run_agent_workflow_tool
from tools.builtin.task_progress_tool import build_task_progress_tool_from_config

pytestmark = [pytest.mark.e2e, pytest.mark.smoke]


@pytest.mark.asyncio
async def test_task_flow_workflow_creates_visual_plan_then_llm_updates_progress(
    tmp_path: Path,
) -> None:
    """验证 task_flow 四步计划创建和模拟执行进度更新。"""
    parent_session_id = "thread-taskflow-e2e"
    cfg = _config(tmp_path)
    manager = _manager(tmp_path, cfg=cfg)
    workflow_tool = build_run_agent_workflow_tool(_bound_handle(manager))

    workflow_result = await workflow_tool.execute(
        {
            "mode": "task_flow",
            "payload": _payload(),
        },
        ToolContext(
            run_id="run-task-flow-create",
            session_id=parent_session_id,
            turn=1,
            call_id="call-task-flow-create",
        ),
    )

    assert workflow_result.ok is True
    assert workflow_result.data is not None
    assert workflow_result.data["mode"] == "task_flow"
    assert workflow_result.data["completed"] is True
    assert "task_flow_plan:" in (workflow_result.content or "")
    assert "task_flow_progress:" in (workflow_result.content or "")

    workflow_dir = Path(str(workflow_result.data["workflow_dir"]))
    task_flow = workflow_result.data["task_flow"]
    plan_path = Path(str(task_flow["plan_path"]))
    progress_path = Path(str(task_flow["progress_path"]))
    nodes_path = Path(str(task_flow["artifact_paths"]["nodes_path"]))

    assert plan_path == workflow_dir / "task_flow" / "plan.json"
    assert progress_path == workflow_dir / "task_flow" / "progress.json"
    assert nodes_path == workflow_dir / "task_flow" / "nodes.jsonl"
    assert (workflow_dir / "workflow.json").is_file()
    assert (workflow_dir / "result.json").is_file()
    assert (workflow_dir / "reports" / "index.json").is_file()
    assert nodes_path.is_file()

    plan = _json(plan_path)
    progress = _json(progress_path)
    workflow = _json(workflow_dir / "workflow.json")
    result_payload = _json(workflow_dir / "result.json")
    nodes = _jsonl(nodes_path)
    audit_actions = [row["action"] for row in _jsonl(workflow_dir / "audit.jsonl")]

    assert plan["objective"] == "改造 Web 任务流体验并接入可视化进度"
    assert plan["title"] == "Task Flow 中等复杂度执行计划"
    assert plan["planning"] == {
        "interaction_mode": "llm_decide",
        "choice_policy": "ask_when_multiple_viable_paths",
        "selected_option": "方案 B：先用轻量任务流打通 UI，再扩展交互式选择",
    }
    assert plan["execution"] == {
        "on_unexpected_severe_issue": "ask_user",
        "progress_tool": "update_task_progress",
        "pause_policy": "stop_and_ask_user",
    }
    assert [node["id"] for node in plan["nodes"]] == [
        "discover-entrypoints",
        "write-spec-contract",
        "wire-progress-ui",
        "verify-and-handoff",
    ]
    assert [node["task_run_id"] for node in plan["nodes"]] == [
        "001-discover-entrypoints",
        "002-write-spec-contract",
        "003-wire-progress-ui",
        "004-verify-and-handoff",
    ]
    assert plan["nodes"][2]["depends_on"] == [
        "discover-entrypoints",
        "write-spec-contract",
    ]
    assert plan["nodes"][1]["metadata"]["requires_user_choice"] is True
    assert plan["nodes"][2]["metadata"]["severe_issue_policy"] == "ask_user"
    assert nodes == plan["nodes"]

    assert progress["status"] == "plan_created"
    assert progress["progress_tool"] == "update_task_progress"
    assert progress["counts"] == {
        "pending": 4,
        "in_progress": 0,
        "completed": 0,
        "total": 4,
    }

    assert workflow["status"] == "plan_created"
    assert workflow["mode"] == "task_flow"
    assert [item["task_id"] for item in workflow["assigned_agents"]] == [
        "discover-entrypoints",
        "write-spec-contract",
        "wire-progress-ui",
        "verify-and-handoff",
    ]
    assert result_payload["task_flow"]["node_count"] == 4
    assert "task_flow.workflow_started" in audit_actions
    assert "task_flow.plan_created" in audit_actions

    session_progress_path = tmp_path / "sessions" / parent_session_id / "task_progress.json"
    workflow_snapshot = _json(session_progress_path)
    assert workflow_snapshot["source"] == "workflow"
    assert workflow_snapshot["counts"] == {
        "pending": 4,
        "in_progress": 0,
        "completed": 0,
        "total": 4,
    }
    assert [task["source_status"] for task in workflow_snapshot["tasks"]] == [
        "assigned",
        "assigned",
        "assigned",
        "assigned",
    ]
    assert {task["orchestration_task_id"] for task in workflow_snapshot["tasks"]} == {
        f"{workflow_result.data['workflow_id']}:{task_run_id}"
        for task_run_id in [
            "001-discover-entrypoints",
            "002-write-spec-contract",
            "003-wire-progress-ui",
            "004-verify-and-handoff",
        ]
    }

    progress_tool = build_task_progress_tool_from_config(cfg)
    in_progress_result = await progress_tool.execute(
        {
            "tasks": _execution_tasks(
                workflow_id=str(workflow_result.data["workflow_id"]),
                plan_nodes=plan["nodes"],
                statuses={
                    "discover-entrypoints": "completed",
                    "write-spec-contract": "completed",
                    "wire-progress-ui": "in_progress",
                    "verify-and-handoff": "pending",
                },
                error_by_task={
                    "wire-progress-ui": ("发现 progress 弹窗缺少阻塞态，需要询问用户后继续。")
                },
            )
        },
        ToolContext(
            run_id="run-task-flow-execute",
            session_id=parent_session_id,
            turn=2,
            call_id="call-task-progress-mid",
        ),
    )

    assert in_progress_result.ok is True
    assert in_progress_result.data is not None
    assert in_progress_result.data["source"] == "llm"
    assert in_progress_result.data["counts"] == {
        "pending": 1,
        "in_progress": 1,
        "completed": 2,
        "total": 4,
    }
    mid_snapshot = _json(session_progress_path)
    assert mid_snapshot["source"] == "llm"
    assert mid_snapshot["tasks"][0]["orchestration_task_id"] == (
        f"{workflow_result.data['workflow_id']}:discover-entrypoints"
    )
    assert mid_snapshot["tasks"][0]["task_run_id"] == "discover-entrypoints"
    assert mid_snapshot["tasks"][2]["status"] == "in_progress"
    assert "阻塞态" in mid_snapshot["tasks"][2]["error_message"]

    completed_result = await progress_tool.execute(
        {
            "tasks": _execution_tasks(
                workflow_id=str(workflow_result.data["workflow_id"]),
                plan_nodes=plan["nodes"],
                statuses={
                    "discover-entrypoints": "completed",
                    "write-spec-contract": "completed",
                    "wire-progress-ui": "completed",
                    "verify-and-handoff": "completed",
                },
            )
        },
        ToolContext(
            run_id="run-task-flow-complete",
            session_id=parent_session_id,
            turn=3,
            call_id="call-task-progress-complete",
        ),
    )

    assert completed_result.ok is True
    assert completed_result.data is not None
    assert completed_result.data["counts"] == {
        "pending": 0,
        "in_progress": 0,
        "completed": 4,
        "total": 4,
    }
    assert completed_result.content == "task progress updated: 4/4 completed"

    completed_snapshot = _json(session_progress_path)
    assert completed_snapshot["source"] == "llm"
    assert [task["status"] for task in completed_snapshot["tasks"]] == [
        "completed",
        "completed",
        "completed",
        "completed",
    ]
    assert [task["display_order"] for task in completed_snapshot["tasks"]] == [0, 1, 2, 3]
    assert all(
        task["workflow_id"] == workflow_result.data["workflow_id"]
        for task in completed_snapshot["tasks"]
    )


def _manager(tmp_path: Path, *, cfg: Config) -> AgentWorkflowManager:
    """构造绑定 task_flow 策略的 workflow manager。"""
    return AgentWorkflowManager(
        subagents=object(),  # type: ignore[arg-type]
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )


def _bound_handle(manager: AgentWorkflowManager) -> AgentWorkflowHandle:
    """构造可供 tool 调用的 workflow handle。"""
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    return handle


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
    """构造中等复杂度 task_flow payload。"""
    return {
        "objective": "改造 Web 任务流体验并接入可视化进度",
        "planning": {
            "interaction_mode": "llm_decide",
            "choice_policy": "ask_when_multiple_viable_paths",
            "selected_option": ("方案 B：先用轻量任务流打通 UI，再扩展交互式选择"),
        },
        "plan": {
            "title": "Task Flow 中等复杂度执行计划",
            "nodes": [
                {
                    "id": "discover-entrypoints",
                    "title": "梳理 workflow 和 progress 入口",
                    "description": (
                        "定位 run_agent_workflow、update_task_progress 和 Progress task UI 的合同。"
                    ),
                    "metadata": {
                        "surface": "backend-contract",
                        "risk": "schema drift",
                    },
                },
                {
                    "id": "write-spec-contract",
                    "title": "写入 task_flow spec 合同",
                    "description": ("说明多方案任务先询问，确认后创建计划并更新进度。"),
                    "depends_on": ["discover-entrypoints"],
                    "metadata": {
                        "requires_user_choice": True,
                        "choice_summary": "用户确认轻量任务流方案。",
                    },
                },
                {
                    "id": "wire-progress-ui",
                    "title": "接入 Progress task 展示",
                    "description": ("把 workflow 计划映射到会话 task_progress.json，供弹窗渲染。"),
                    "depends_on": ["discover-entrypoints", "write-spec-contract"],
                    "metadata": {
                        "surface": "web-progress-popover",
                        "severe_issue_policy": "ask_user",
                    },
                },
                {
                    "id": "verify-and-handoff",
                    "title": "验证并交付任务描述",
                    "description": "用离线 e2e 固定产物、进度和 handoff 文本。",
                    "depends_on": ["wire-progress-ui"],
                    "metadata": {
                        "surface": "tests",
                        "handoff_required": True,
                    },
                },
            ],
        },
        "execution": {
            "on_unexpected_severe_issue": "ask_user",
            "progress_tool": "update_task_progress",
            "pause_policy": "stop_and_ask_user",
        },
        "audit_tags": ["task_flow", "progress_task", "user_choice"],
    }


def _execution_tasks(
    *,
    workflow_id: str,
    plan_nodes: list[dict[str, Any]],
    statuses: dict[str, str],
    error_by_task: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """构造 update_task_progress 参数。"""
    errors = error_by_task or {}
    tasks: list[dict[str, Any]] = []
    for index, node in enumerate(plan_nodes):
        task_id = str(node["id"])
        item: dict[str, Any] = {
            "workflow_id": workflow_id,
            "step_id": task_id,
            "desc": str(node["title"]),
            "status": statuses[task_id],
            "display_order": index,
            "source_status": "llm_execution",
        }
        if task_id in errors:
            item["error_message"] = errors[task_id]
        tasks.append(item)
    return tasks


def _json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件，输入为路径，输出为 dict。"""
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件，输入为路径，输出为 dict 列表。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
