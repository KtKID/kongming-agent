"""Deep Research 注入 source provider workflow e2e 测试。

本脚本验证 AgentWorkflowManager 注入用户 provider 后的 Search 到 Extract 产物流转。
作用是固定 workflow result、sources、facts、phase summary 和 report 中的 source_provider、provider_name 与 URL 可追踪合同。
关键执行流程：构造 fake 用户 provider，经 run_agent_workflow tool 执行 deep_research，读取 artifact 断言来源 URL 进入 Extract 和 Report 阶段。
关键函数：test_deep_research_workflow_uses_injected_provider_sources 覆盖注入 provider smoke 主链路。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from application.agent_roles import AgentRoleManager
from application.agent_workflows.strategies.deep_research import (
    ResearchSourceCandidate,
    ResearchSourceQuery,
    ResearchSourceRecord,
)
from core.contracts import ToolContext
from infrastructure.config.models import Config, ModelSelectionConfig
from tests.support.tool_calls import execute_prepared_tool
from tests.support.workflow_strategy_manager import WorkflowStrategyTestManager
from tools.agent_workflow_tool import AgentWorkflowHandle, build_run_agent_workflow_tool

pytestmark = [pytest.mark.e2e, pytest.mark.smoke]


@pytest.mark.asyncio
async def test_deep_research_workflow_uses_injected_provider_sources(tmp_path: Path) -> None:
    """验证注入 provider，输入为 fake 用户来源，输出为 result 和 artifact 中的来源字段。"""
    provider = _InjectedUserSourceProvider()
    manager = _manager(tmp_path, source_provider=provider)
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    tool_result = await execute_prepared_tool(
        tool,
        {"mode": "deep_research", "payload": _payload()},
        ToolContext(
            run_id="run-injected-provider",
            session_id="parent-session",
            turn=1,
            call_id="call-injected-provider",
        ),
    )

    assert tool_result.ok is True
    assert tool_result.data is not None
    workflow_dir = Path(str(tool_result.data["workflow_dir"]))
    board_dir = workflow_dir / "deep_research"
    result_payload = json.loads((workflow_dir / "result.json").read_text(encoding="utf-8"))
    stats = json.loads((board_dir / "stats.json").read_text(encoding="utf-8"))
    sources = _jsonl(board_dir / "sources.jsonl")
    selected_sources = _jsonl(board_dir / "sources.selected.jsonl")
    facts = _jsonl(board_dir / "facts.jsonl")
    phase_summaries_payload = json.loads(
        (board_dir / "phase_summaries.json").read_text(encoding="utf-8")
    )
    phase_summaries = phase_summaries_payload["phases"]
    report_markdown = (board_dir / "report.md").read_text(encoding="utf-8")

    assert result_payload["deep_research"]["source_provider"] == "injected_user_source"
    assert stats["source_provider"] == "injected_user_source"
    assert sources[0]["provider_name"] == "injected_user_source"
    assert sources[0]["url"] == "https://example.com/injected-source"
    assert selected_sources[0]["url"] == "https://example.com/injected-source"
    assert facts[0]["source_id"] == sources[0]["source_id"]
    assert "https://example.com/injected-source" in facts[0]["citation"]
    assert "https://example.com/injected-source" in report_markdown
    assert provider.search_queries == ["Search to Extract provider URL"]
    assert provider.fetched_urls == ["https://example.com/injected-source"]

    search_summary = _phase(phase_summaries, "search")
    extract_summary = _phase(phase_summaries, "extract")
    assert search_summary["metadata"]["provider_name"] == "injected_user_source"
    assert str(search_summary["output_artifacts"][1]).endswith("sources.selected.jsonl")
    assert str(extract_summary["output_artifacts"][0]).endswith("facts.jsonl")


@pytest.mark.asyncio
async def test_deep_research_workflow_records_missing_provider_diagnostics(
    tmp_path: Path,
) -> None:
    """验证缺失 provider 诊断，输入为空 provider 和 fallback reason，输出 result/audit 可追踪。"""
    diagnostics = {
        "enabled": True,
        "provider_name": "web_user_tool_research_source",
        "search_tool_name": None,
        "fetch_tool_name": None,
        "reason": "search_tool_missing",
        "missing_tools": ("web_search",),
        "fallback_reason": "no configured or default search tool is registered",
    }
    manager = _manager(tmp_path, source_provider=None, diagnostics=diagnostics)
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    tool_result = await execute_prepared_tool(
        tool,
        {
            "mode": "deep_research",
            "payload": {
                "topic": "Missing web provider diagnostics",
            },
        },
        ToolContext(
            run_id="run-missing-provider",
            session_id="parent-session",
            turn=1,
            call_id="call-missing-provider",
        ),
    )

    assert tool_result.ok is True
    assert tool_result.data is not None
    workflow_dir = Path(str(tool_result.data["workflow_dir"]))
    result_payload = json.loads((workflow_dir / "result.json").read_text(encoding="utf-8"))
    audit_rows = _jsonl(workflow_dir / "audit.jsonl")
    diagnostic_events = [
        row for row in audit_rows if row["action"] == "deep_research.source_provider_diagnostics"
    ]

    result_diagnostics = result_payload["deep_research"]["source_provider_diagnostics"]
    assert result_payload["deep_research"]["source_provider"] == "deterministic_research_source"
    assert result_diagnostics["reason"] == "search_tool_missing"
    assert result_diagnostics["fallback_reason"] == (
        "no configured or default search tool is registered"
    )
    assert diagnostic_events[0]["payload"]["reason"] == "search_tool_missing"
    assert diagnostic_events[0]["payload"]["missing_tools"] == ["web_search"]


class _InjectedUserSourceProvider:
    """测试用用户来源 provider，返回固定 URL 和正文。"""

    name = "injected_user_source"

    def __init__(self) -> None:
        """初始化 provider，输入为空，输出为记录 search/fetch 调用的实例。"""
        self.search_queries: list[str] = []
        self.fetched_urls: list[str] = []

    async def search(self, query: ResearchSourceQuery) -> tuple[ResearchSourceCandidate, ...]:
        """返回固定候选，输入为搜索线，输出为来自用户 provider 的候选 URL。"""
        self.search_queries.append(query.line)
        return (
            ResearchSourceCandidate(
                source_id="",
                query_id=query.query_id,
                url="https://example.com/injected-source",
                canonical_url="",
                title="Injected User Source",
                snippet="Injected source snippet",
                rank=1,
                provider_name=self.name,
            ),
        )

    async def fetch(self, candidate: ResearchSourceCandidate) -> ResearchSourceRecord:
        """返回固定正文，输入为候选 URL，输出为 fetched strong 来源记录。"""
        self.fetched_urls.append(candidate.url)
        return ResearchSourceRecord(
            source_id=candidate.source_id,
            query_id=candidate.query_id,
            url=candidate.url,
            canonical_url=candidate.canonical_url,
            title=candidate.title,
            status="fetched",
            tier="strong",
            content_text="Injected source content proves URL flow from Search to Extract.",
            error_code=None,
            error_message=None,
            provider_name=self.name,
            rank=candidate.rank,
        )


class _NoWorkflowTaskExecutor:
    """测试用 task executor，记录意外 child 调用。"""

    def __init__(self) -> None:
        """初始化 manager，输入为空，输出为可记录任务的实例。"""
        self.tasks: list[Any] = []

    async def execute_task(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        task: Any,
        audit_writer: Any,
    ) -> Any:
        """记录意外子任务，输入为任务上下文，输出为测试失败。"""
        del workflow_id, parent_session_id, audit_writer
        self.tasks.append(task)
        raise AssertionError(
            "deep_research fallback should write phase logs without subagent calls"
        )


def _manager(
    tmp_path: Path,
    *,
    source_provider: _InjectedUserSourceProvider | None,
    diagnostics: dict[str, object] | None = None,
) -> WorkflowStrategyTestManager:
    """构造 workflow manager，输入为临时目录和 provider，输出为 manager。"""
    return WorkflowStrategyTestManager(
        task_executor=_NoWorkflowTaskExecutor(),
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        deep_research_source_provider=source_provider,
        deep_research_source_diagnostics=diagnostics,
    )


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 Config。"""
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    cfg.session.file_store_path = str(tmp_path / "sessions")
    return cfg


def _payload() -> dict[str, Any]:
    """构造 deep_research payload，输入为空，输出为单来源低预算 payload。"""
    return {
        "topic": "Injected source provider workflow",
        "source_queries": [
            {
                "query_id": "q-injected",
                "line": "Search to Extract provider URL",
                "intent": "overview",
                "max_results": 1,
            }
        ],
        "limits": {
            "jury_size": 3,
            "reject_quorum": 2,
            "source_budget": 1,
            "fetch_budget": 1,
            "fact_cap": 1,
            "search_results_per_line": 1,
            "fetch_concurrency": 1,
            "jury_concurrency": 1,
            "workflow_timeout_seconds": 2400,
        },
        "output_contract": "deep_research_report",
    }


def _jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL，输入为路径，输出为 dict 列表。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _phase(rows: list[dict[str, Any]], phase: str) -> dict[str, Any]:
    """读取阶段摘要，输入为 summaries 和阶段名，输出为匹配行。"""
    return next(row for row in rows if row["phase"] == phase)
