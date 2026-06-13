"""deep_research workflow tool 单元测试。

本脚本验证 run_agent_workflow 对 deep_research 的 mode schema、payload 默认值归一化和 tool 到 manager 的分流。
作用是固定 Deep Research 从 LLM tool call 进入 workflow runtime 的最小合同。
关键执行流程：读取工具 schema，调用 payload normalizer，再用 fake manager 捕获最终 payload。
关键函数：_minimal_payload 构造最小研究输入，test_* 覆盖 schema、normalize 和 execute 分流。
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


def test_run_agent_workflow_schema_exposes_deep_research_mode_and_payload_fields() -> None:
    """验证 tool schema，输入为默认 tool，输出为 deep_research mode 和 payload 字段断言。"""
    tool = build_run_agent_workflow_tool(AgentWorkflowHandle())

    mode_schema = tool.input_schema["properties"]["mode"]
    payload_schema = tool.input_schema["properties"]["payload"]
    payload_properties = payload_schema["properties"]

    assert "deep_research" in mode_schema["enum"]
    assert {"topic", "objective", "limits", "source_policy", "output_contract"}.issubset(
        payload_properties
    )
    assert payload_properties["output_contract"]["enum"] == ["deep_research_report"]


def test_normalize_deep_research_payload_fills_defaults() -> None:
    """验证 payload 默认值，输入为最小 topic，输出为 limits/source_policy/output_contract。"""
    normalized = _normalize_workflow_payload(
        "deep_research",
        {"topic": "  Kongming agent workflow research  "},
        workspace_root=Path("/tmp/workspace"),
    )

    assert normalized["mode"] == "deep_research"
    assert normalized["topic"] == "Kongming agent workflow research"
    assert normalized["objective"] == "Kongming agent workflow research"
    assert normalized["output_contract"] == "deep_research_report"
    assert normalized["limits"] == {
        "jury_size": 3,
        "reject_quorum": 2,
        "source_budget": 10,
        "fetch_budget": 10,
        "fact_cap": 20,
        "max_content_chars": 60000,
        "search_results_per_line": 6,
        "fetch_concurrency": 4,
        "jury_concurrency": 6,
        "workflow_timeout_seconds": 2400,
    }
    assert normalized["source_policy"] == {
        "provider": "internal",
        "language": "zh-CN",
        "freshness_days": None,
        "allowed_domains": [],
        "blocked_domains": [],
        "prefer_primary_sources": True,
    }


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_passes_normalized_deep_research_payload() -> None:
    """验证 tool 分流，输入为 deep_research 最小 payload，输出为 manager 捕获的默认字段。"""
    manager = _CapturingManager()
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    result = await tool.execute(
        {
            "mode": "deep_research",
            "payload": _minimal_payload(),
        },
        ToolContext(run_id="run-1", session_id="parent-session", turn=1, call_id="call-1"),
    )

    assert result.ok is True
    assert manager.mode == "deep_research"
    assert manager.parent_session_id == "parent-session"
    assert manager.payload["limits"]["source_budget"] == 10
    assert manager.payload["limits"]["fact_cap"] == 20
    assert manager.payload["source_policy"]["prefer_primary_sources"] is True
    assert manager.payload["output_contract"] == "deep_research_report"
    assert result.data is not None
    assert result.data["mode"] == "deep_research"


@pytest.mark.asyncio
async def test_run_agent_workflow_tool_formats_deep_research_payload_errors() -> None:
    """验证 deep_research 参数错误提示，输入为非法 payload，输出为专用修正骨架。"""
    handle = AgentWorkflowHandle()
    handle.bind(_CapturingManager())
    tool = build_run_agent_workflow_tool(handle)

    result = await tool.execute(
        {
            "mode": "deep_research",
            "payload": "bad-payload",
        },
        ToolContext(run_id="run-1", session_id="parent-session", turn=1, call_id="call-1"),
    )

    assert result.ok is False
    assert result.content is not None
    assert "run_agent_workflow deep_research 参数修正提示" in result.content
    assert '"mode": "deep_research"' in result.content
    assert '"source_queries"' in result.content
    assert '"output_contract": "deep_research_report"' in result.content


def _minimal_payload() -> dict[str, object]:
    """构造最小 deep_research payload，输入为空，输出为 topic-only 字典。"""
    return {"topic": "Kongming Deep Research workflow"}


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
    workflow_dir = Path("/tmp/wf-deep-research")
    return AgentWorkflowResult(
        workflow_id="wf-deep-research",
        mode="deep_research",
        parent_session_id="parent-session",
        workflow_dir=workflow_dir,
        started_at="2026-06-12T00:00:00+00:00",
        finished_at="2026-06-12T00:00:01+00:00",
        runs=(),
        reports=(),
        report_index_path=workflow_dir / "reports" / "index.json",
        completed_override=True,
        data={
            "deep_research": {
                "artifact_paths": {"report_path": str(workflow_dir / "deep_research" / "report.md")}
            }
        },
    )
