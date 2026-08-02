"""Task Flow deterministic e2e 测试。

本脚本验证 run_agent_workflow tool、AgentWorkflowManager、TaskFlowStrategy 和
advance_task_progress 工具的完整离线链路。
作用是固定 task_flow 的中等复杂度合同：先计划，再由 LLM 推进进度。
关键流程：从 tool call 创建四步任务计划，读取 workflow/task_flow/session 产物，
再模拟主 LLM 逐步调用 start/next 命令完成任务。
关键函数覆盖计划创建和执行进度更新主链路。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from application.agent_roles import AgentRoleManager
from application.agent_workflows.manager import AgentWorkflowManager
from core.contracts import ToolContext
from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.threads.metadata import ThreadMetadata
from infrastructure.config.models import Config, ModelSelectionConfig
from sessions import SessionTaskProgressManager
from tests.support.tool_calls import execute_prepared_tool
from tests.support.workflow_strategy_manager import WorkflowStrategyTestManager
from tests.unit.test_web_app_lifespan import _seed_password
from tools.agent_workflow_tool import AgentWorkflowHandle, build_run_agent_workflow_tool
from tools.builtin.task_progress_tool import build_task_progress_tool_from_config

pytestmark = [pytest.mark.e2e, pytest.mark.smoke]


@pytest.mark.asyncio
async def test_task_flow_workflow_creates_visual_plan_then_llm_advances_progress(
    tmp_path: Path,
) -> None:
    """验证 task_flow 四步计划创建和受限 start/next 进度推进。"""
    parent_session_id = "thread-abcdef123456"
    cfg = _config(tmp_path)
    manager = _manager(tmp_path, cfg=cfg)
    workflow_tool = build_run_agent_workflow_tool(_bound_handle(manager))

    workflow_result = await execute_prepared_tool(
        workflow_tool,
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
        "progress_tool": "advance_task_progress",
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

    assert progress["status"] == "initial_plan"
    assert progress["progress_tool"] == "advance_task_progress"
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
    assert workflow_snapshot["schema_version"] == 2
    assert workflow_snapshot["workflow_id"] == workflow_result.data["workflow_id"]
    assert workflow_snapshot["control_mode"] == "llm_steps"
    assert workflow_snapshot["counts"] == {
        "pending": 4,
        "in_progress": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "total": 4,
    }
    assert [task["task_id"] for task in workflow_snapshot["tasks"]] == [
        "discover-entrypoints",
        "write-spec-contract",
        "wire-progress-ui",
        "verify-and-handoff",
    ]
    assert workflow_snapshot["tasks"][2]["depends_on"] == [
        "discover-entrypoints",
        "write-spec-contract",
    ]

    progress_tool = build_task_progress_tool_from_config(cfg)
    workflow_id = str(workflow_result.data["workflow_id"])
    for call_id, arguments in enumerate(
        [
            _progress_command("start", workflow_id, "discover-entrypoints"),
            _progress_command("next", workflow_id, "discover-entrypoints", "write-spec-contract"),
            _progress_command("next", workflow_id, "write-spec-contract", "wire-progress-ui"),
        ],
        1,
    ):
        in_progress_result = await execute_prepared_tool(
            progress_tool,
            arguments,
            ToolContext(
                run_id="run-task-flow-execute",
                session_id=parent_session_id,
                turn=2,
                call_id=f"call-task-progress-mid-{call_id}",
            ),
        )
        assert in_progress_result.ok is True

    assert in_progress_result.ok is True
    assert in_progress_result.data is not None
    assert in_progress_result.data["counts"] == {
        "pending": 1,
        "in_progress": 1,
        "completed": 2,
        "failed": 0,
        "cancelled": 0,
        "total": 4,
    }
    mid_snapshot = _json(session_progress_path)
    assert mid_snapshot["workflow_id"] == workflow_id
    assert mid_snapshot["tasks"][0]["task_run_id"] == "001-discover-entrypoints"
    assert mid_snapshot["tasks"][2]["status"] == "in_progress"
    assert mid_snapshot["tasks"][2]["error_message"] is None

    completed_result = await execute_prepared_tool(
        progress_tool,
        _progress_command("next", workflow_id, "wire-progress-ui", "verify-and-handoff"),
        ToolContext(
            run_id="run-task-flow-complete",
            session_id=parent_session_id,
            turn=3,
            call_id="call-task-progress-complete",
        ),
    )

    assert completed_result.ok is True
    completed_result = await execute_prepared_tool(
        progress_tool,
        _progress_command("next", workflow_id, "verify-and-handoff"),
        ToolContext(
            run_id="run-task-flow-complete",
            session_id=parent_session_id,
            turn=4,
            call_id="call-task-progress-final",
        ),
    )
    assert completed_result.ok is True
    assert completed_result.data is not None
    assert completed_result.data["counts"] == {
        "pending": 0,
        "in_progress": 0,
        "completed": 4,
        "failed": 0,
        "cancelled": 0,
        "total": 4,
    }
    assert "task progress advanced: 4/4 completed" in completed_result.content

    completed_snapshot = _json(session_progress_path)
    assert [task["status"] for task in completed_snapshot["tasks"]] == [
        "completed",
        "completed",
        "completed",
        "completed",
    ]
    assert [task["display_order"] for task in completed_snapshot["tasks"]] == [0, 1, 2, 3]
    assert _json(plan_path) == plan

    workflow_b_payload = _payload()
    workflow_b_plan = workflow_b_payload["plan"]
    assert isinstance(workflow_b_plan, dict)
    workflow_b_plan["title"] = "B 接管后的发布检查"
    workflow_b_result = await execute_prepared_tool(
        workflow_tool,
        {"mode": "task_flow", "payload": workflow_b_payload},
        ToolContext(
            run_id="run-task-flow-create-b",
            session_id=parent_session_id,
            turn=5,
            call_id="call-task-flow-create-b",
        ),
    )
    assert workflow_b_result.ok is True
    assert workflow_b_result.data is not None
    workflow_b_id = str(workflow_b_result.data["workflow_id"])
    started_b = await execute_prepared_tool(
        progress_tool,
        _progress_command("start", workflow_b_id, "discover-entrypoints"),
        ToolContext(
            run_id="run-task-flow-start-b",
            session_id=parent_session_id,
            turn=6,
            call_id="call-task-flow-start-b",
        ),
    )
    stale_a = await execute_prepared_tool(
        progress_tool,
        _progress_command("start", workflow_id, "discover-entrypoints"),
        ToolContext(
            run_id="run-task-flow-late-a",
            session_id=parent_session_id,
            turn=7,
            call_id="call-task-flow-late-a",
        ),
    )
    assert started_b.ok is True
    assert stale_a.ok is False
    foreground_b_snapshot = _json(session_progress_path)
    assert foreground_b_snapshot["workflow_id"] == workflow_b_id
    assert foreground_b_snapshot["title"] == "B 接管后的发布检查"
    assert foreground_b_snapshot["tasks"][0]["status"] == "in_progress"
    assert _json(plan_path) == plan

    _seed_password(tmp_path, "pwd")
    app = create_app(
        cfg,
        _TaskFlowThreadManager(parent_session_id),
        home_dir=tmp_path,
        task_progress_manager=SessionTaskProgressManager.from_config(cfg),
    )
    client = TestClient(app)
    client.__enter__()
    try:
        login = client.post(
            "/api/auth/login",
            json={"password": "pwd"},
            headers={CSRF_HEADER_NAME: CSRF_HEADER_VALUE},
        )
        response = client.get(f"/api/threads/{parent_session_id}/task-progress")
        assert login.status_code == 200
        assert response.status_code == 200
        assert response.json()["workflow_id"] == workflow_b_id
        assert response.json()["tasks"] == foreground_b_snapshot["tasks"]
    finally:
        client.__exit__(None, None, None)


def _manager(tmp_path: Path, *, cfg: Config) -> WorkflowStrategyTestManager:
    """构造绑定 task_flow 策略的 workflow manager。"""
    return WorkflowStrategyTestManager(
        config=cfg,
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
    )


class _TaskFlowThreadManager:
    """为 task-flow→REST e2e 提供最小 thread 查询门户。"""

    def __init__(self, thread_id: str) -> None:
        """构造可查询 thread，输入为 thread ID，输出为空。"""
        self.usage_manager = object()
        self._thread = ThreadMetadata(
            id=thread_id,
            name="task-flow e2e",
            preset_id="p",
            backend_kind="generic_chat",
            cwd="",
            created_at=1.0,
            updated_at=1.0,
            message_count=0,
        )

    async def start(self) -> None:
        """满足 Web 应用启动协议，输入为空，输出为空。"""

    async def aclose_all(self) -> None:
        """满足 Web 应用关闭协议，输入为空，输出为空。"""

    def list_threads(self) -> list[ThreadMetadata]:
        """返回 task-flow thread，输入为空，输出为单元素列表。"""
        return [self._thread]

    def __getattr__(self, name: str) -> Any:
        """拒绝本 e2e 未调用的 ThreadManager 能力，输入为属性名，输出为 AttributeError。"""
        raise AttributeError(name)


def _bound_handle(manager: AgentWorkflowManager) -> AgentWorkflowHandle:
    """构造可供 tool 调用的 workflow handle。"""
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    return handle


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 file session 配置。"""
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
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
                        "定位 run_agent_workflow、advance_task_progress 和 Progress task UI 的合同。"
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
            "pause_policy": "stop_and_ask_user",
        },
        "audit_tags": ["task_flow", "progress_task", "user_choice"],
    }


def _progress_command(
    action: str,
    workflow_id: str,
    step_id: str,
    next_step_id: str | None = None,
) -> dict[str, str]:
    """构造受限进度命令，输入为动作与步骤坐标，输出为 tool 参数。"""
    command = {"action": action, "workflow_id": workflow_id, "step_id": step_id}
    if next_step_id is not None:
        command["next_step_id"] = next_step_id
    return command


def _json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件，输入为路径，输出为 dict。"""
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件，输入为路径，输出为 dict 列表。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
