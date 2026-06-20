"""task_flow workflow tool 单元测试。

本脚本验证 run_agent_workflow 对 task_flow 的 mode schema、payload 默认值归一化和 tool 到 manager 的分流。
作用是固定 LLM 从 tool call 创建 Task Flow 计划的最小合同。
关键执行流程：读取工具 schema，调用 payload normalizer，再用 fake manager 捕获最终 payload。
关键函数：_minimal_payload 构造最小计划输入，test_* 覆盖 schema、normalize 和 execute 分流。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from application.agent_workflows.manager import AgentWorkflowResult
from core.contracts import ToolContext
from tools.agent_workflow_tool import (
    AgentWorkflowHandle,
    _normalize_workflow_payload,
    build_run_agent_workflow_tool,
)


def test_run_agent_workflow_schema_exposes_task_flow_payload_fields() -> None:
    """验证 tool schema，输入为默认 tool，输出为开放 mode 和 task_flow payload 字段断言。"""
    tool = build_run_agent_workflow_tool(AgentWorkflowHandle())

    mode_schema = tool.input_schema["properties"]["mode"]
    payload_schema = tool.input_schema["properties"]["payload"]
    payload_properties = payload_schema["properties"]

    assert mode_schema["type"] == "string"
    assert "enum" not in mode_schema
    assert {"objective", "planning", "plan", "execution"}.issubset(payload_properties)
    assert payload_properties["plan"]["properties"]["nodes"]["items"]["required"] == ["title"]


def test_normalize_task_flow_payload_fills_defaults_and_steps_alias() -> None:
    """验证 payload 默认值，输入为 steps 别名，输出为 plan.nodes 和执行默认值。"""
    normalized = _normalize_workflow_payload(
        "task_flow",
        {
            "objective": "  完成用户任务  ",
            "plan": {
                "title": "任务执行计划",
                "steps": [{"title": "建立计划"}],
            },
        },
        workspace_root=Path("/tmp/workspace"),
    )

    assert normalized["mode"] == "task_flow"
    assert normalized["objective"] == "完成用户任务"
    assert normalized["planning"] == {
        "interaction_mode": "llm_decide",
        "choice_policy": "ask_when_multiple_viable_paths",
    }
    assert normalized["execution"] == {
        "on_unexpected_severe_issue": "ask_user",
        "progress_tool": "update_task_progress",
    }
    assert normalized["plan"]["nodes"] == [{"title": "建立计划"}]


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_passes_normalized_task_flow_payload() -> None:
    """验证 tool 分流，输入为 task_flow 最小 payload，输出为 manager 捕获的默认字段。"""
    manager = _CapturingManager()
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    result = await tool.execute(
        {
            "mode": "task_flow",
            "payload": _minimal_payload(),
        },
        ToolContext(run_id="run-1", session_id="parent-session", turn=1, call_id="call-1"),
    )

    assert result.ok is True
    assert manager.mode == "task_flow"
    assert manager.parent_session_id == "parent-session"
    assert manager.payload["planning"]["interaction_mode"] == "llm_decide"
    assert manager.payload["execution"]["progress_tool"] == "update_task_progress"
    assert result.content is not None
    assert "task_flow_plan" in result.content
    assert result.data is not None
    assert result.data["task_flow"]["plan_path"].endswith("plan.json")


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_formats_task_flow_payload_errors() -> None:
    """验证 task_flow 参数错误提示，输入为非法 payload，输出为专用修正骨架。"""
    handle = AgentWorkflowHandle()
    handle.bind(_CapturingManager())
    tool = build_run_agent_workflow_tool(handle)

    result = await tool.execute(
        {
            "mode": "task_flow",
            "payload": "bad-payload",
        },
        ToolContext(run_id="run-1", session_id="parent-session", turn=1, call_id="call-1"),
    )

    assert result.ok is False
    assert result.content is not None
    assert "run_agent_workflow task_flow 参数修正提示" in result.content
    assert '"mode": "task_flow"' in result.content
    assert '"objective"' in result.content
    assert '"plan"' in result.content


def _minimal_payload() -> dict[str, object]:
    """构造最小 task_flow payload，输入为空，输出为 objective 和 plan 字典。"""
    return {
        "objective": "完成用户任务",
        "plan": {
            "title": "任务执行计划",
            "nodes": [{"id": "step-1", "title": "建立计划"}],
        },
    }


class _CapturingManager:
    """测试用 workflow manager，捕获 run_workflow_payload 入参并返回 fake 结果。"""

    workspace_root = Path("/tmp/workspace")

    def __init__(self) -> None:
        """初始化 fake manager，输入为空，输出为可捕获调用的实例。"""
        self.mode = ""
        self.parent_session_id = ""
        self.payload: dict[str, Any] = {}

    async def run_workflow_payload(
        self,
        *,
        mode: str,
        parent_session_id: str,
        payload: dict[str, Any],
    ) -> AgentWorkflowResult:
        """记录 workflow payload 调用，输入为 mode/session/payload，输出为 fake 结果。"""
        self.mode = mode
        self.parent_session_id = parent_session_id
        self.payload = payload
        return _fake_result()


def _fake_result() -> AgentWorkflowResult:
    """构造 fake workflow result，输入为空，输出为 tool 可格式化的结果。"""
    workflow_dir = Path("/tmp/wf-task-flow")
    return AgentWorkflowResult(
        workflow_id="wf-task-flow",
        mode="task_flow",
        parent_session_id="parent-session",
        workflow_dir=workflow_dir,
        started_at="2026-06-18T00:00:00+00:00",
        finished_at="2026-06-18T00:00:01+00:00",
        runs=(),
        reports=(),
        report_index_path=workflow_dir / "reports" / "index.json",
        data={
            "task_flow": {
                "plan_path": str(workflow_dir / "task_flow" / "plan.json"),
                "progress_path": str(workflow_dir / "task_flow" / "progress.json"),
            }
        },
        completed_override=True,
    )
