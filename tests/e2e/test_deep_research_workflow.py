"""Deep Research deterministic e2e 测试。

本脚本验证 run_agent_workflow tool 到 AgentWorkflowManager、DeepResearchStrategy、fake source provider 和 artifacts 的完整离线链路。
作用是用无网络、无真实模型的方式固定 deep_research v0.1 必须写出的 root result、reports index、report.md、stats 和 audit。
关键执行流程：绑定 fake source provider 或默认 provider，构造 subagent 哨兵，从 tool 入口运行 deep_research，读取 workflow 目录产物断言。
关键函数：test_deep_research_workflow_smoke_runs_tool_to_artifacts 是离线 smoke 主链路。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from application.agent_roles import AgentRoleManager
from application.agent_workflows.manager import AgentWorkflowManager
from application.agent_workflows.strategies.deep_research import FakeResearchSourceProvider
from core.contracts import ToolContext
from infrastructure.config.models import Config, ModelConfig
from tools.agent_workflow_tool import AgentWorkflowHandle, build_run_agent_workflow_tool

pytestmark = [pytest.mark.e2e, pytest.mark.smoke]


@pytest.mark.asyncio
async def test_deep_research_workflow_smoke_runs_tool_to_artifacts(tmp_path: Path) -> None:
    """验证完整离线链路，输入为 tool call，输出为 workflow root 和 deep_research 产物。"""
    subagents = _DeterministicSubAgentManager()
    manager = _manager(tmp_path, subagents=subagents, source_provider=_source_provider())
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    tool_result = await tool.execute(
        {"mode": "deep_research", "payload": _payload()},
        ToolContext(run_id="run-e2e", session_id="parent-session", turn=1, call_id="call-e2e"),
    )

    assert tool_result.ok is True
    assert tool_result.data is not None
    assert tool_result.data["mode"] == "deep_research"
    workflow_dir = Path(str(tool_result.data["workflow_dir"]))
    board_dir = workflow_dir / "deep_research"

    assert (workflow_dir / "result.json").is_file()
    assert (workflow_dir / "reports" / "index.json").is_file()
    assert (board_dir / "report.md").is_file()
    assert (board_dir / "stats.json").is_file()
    assert (workflow_dir / "audit.jsonl").is_file()

    result_payload = json.loads((workflow_dir / "result.json").read_text(encoding="utf-8"))
    report_index = json.loads((workflow_dir / "reports" / "index.json").read_text(encoding="utf-8"))
    stats = json.loads((board_dir / "stats.json").read_text(encoding="utf-8"))
    checked_groups = _jsonl(board_dir / "groups.checked.jsonl")
    report_markdown = (board_dir / "report.md").read_text(encoding="utf-8")
    actions = [
        json.loads(line)["action"]
        for line in (workflow_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    checked_statuses = {row["ruling"]["status"] for row in checked_groups}

    assert result_payload["mode"] == "deep_research"
    assert result_payload["completed"] is True
    assert report_index["mode"] == "deep_research"
    assert stats["selected_source_count"] <= _payload()["limits"]["source_budget"]
    assert stats["top_fact_count"] <= _payload()["limits"]["fact_cap"]
    assert checked_statuses == {"upheld", "rejected"}
    assert "tally: 3-0" in report_markdown
    assert "tally: 0-3" in report_markdown
    assert "deep_research.workflow_started" in actions
    assert "deep_research.subagent_task_started" in actions
    assert "deep_research.subagent_task_completed" in actions
    assert "deep_research_completed" in actions
    assert _task_log_paths_are_readable(workflow_dir)


@pytest.mark.asyncio
async def test_deep_research_workflow_topic_only_uses_default_provider(tmp_path: Path) -> None:
    """验证默认 provider 链路，输入为 topic-only tool call，输出为确定性来源产物。"""
    subagents = _DeterministicSubAgentManager()
    manager = _manager(tmp_path, subagents=subagents)
    handle = AgentWorkflowHandle()
    handle.bind(manager)
    tool = build_run_agent_workflow_tool(handle)

    tool_result = await tool.execute(
        {"mode": "deep_research", "payload": {"topic": "Default provider research"}},
        ToolContext(
            run_id="run-default", session_id="parent-session", turn=1, call_id="call-default"
        ),
    )

    assert tool_result.ok is True
    assert tool_result.data is not None
    workflow_dir = Path(str(tool_result.data["workflow_dir"]))
    board_dir = workflow_dir / "deep_research"
    result_payload = json.loads((workflow_dir / "result.json").read_text(encoding="utf-8"))
    stats = json.loads((board_dir / "stats.json").read_text(encoding="utf-8"))
    report_markdown = (board_dir / "report.md").read_text(encoding="utf-8")

    assert result_payload["deep_research"]["source_provider"] == "deterministic_research_source"
    assert stats["fetched_source_count"] >= 1
    assert "Default provider research" in report_markdown
    assert subagents.tasks == []
    assert _task_log_paths_are_readable(workflow_dir)


class _DeterministicSubAgentManager:
    """测试用子 agent manager，记录 fallback 链路里的意外子 agent 调用。"""

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
    ) -> Any:
        """记录意外子任务调用，输入为任务上下文，输出为测试失败。"""
        del workflow_id, parent_session_id, audit_writer
        self.tasks.append(task)
        raise AssertionError(
            "deep_research fallback should write phase logs without subagent calls"
        )


def _manager(
    tmp_path: Path,
    *,
    subagents: object,
    source_provider: FakeResearchSourceProvider | None = None,
) -> AgentWorkflowManager:
    """构造 workflow manager，输入为临时目录和 fake subagents，输出为 manager。"""
    return AgentWorkflowManager(
        subagents=subagents,  # type: ignore[arg-type]
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        deep_research_source_provider=source_provider,
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
    """构造 deep_research payload，输入为空，输出为低预算确定性 payload。"""
    return {
        "topic": "Kongming Deep Research workflow",
        "source_policy": {
            "language": "zh-CN",
            "freshness_days": None,
            "allowed_domains": [],
            "blocked_domains": [],
            "prefer_primary_sources": True,
        },
        "source_queries": [
            {
                "query_id": "L-001",
                "line": "query L-001",
                "intent": "overview",
                "max_results": 3,
            },
            {
                "query_id": "L-002",
                "line": "query L-002",
                "intent": "skeptical",
                "max_results": 3,
            },
            {
                "query_id": "L-003",
                "line": "query L-003",
                "intent": "practical",
                "max_results": 3,
            },
        ],
        "limits": {
            "jury_size": 3,
            "reject_quorum": 2,
            "source_budget": 3,
            "fetch_budget": 3,
            "fact_cap": 3,
            "search_results_per_line": 3,
            "fetch_concurrency": 2,
            "jury_concurrency": 3,
            "workflow_timeout_seconds": 2400,
        },
        "output_contract": "deep_research_report",
    }


def _source_provider() -> FakeResearchSourceProvider:
    """构造 fake source provider，输入为空，输出为 search/fetch fixture。"""
    return FakeResearchSourceProvider(
        search_index={
            "L-001": [
                {"url": "https://example.com/a", "title": "A"},
                {"url": "https://www.example.com/a/", "title": "A duplicate"},
            ],
            "L-002": [{"url": "https://example.com/b", "title": "B"}],
            "L-003": [{"url": "https://example.com/c", "title": "C"}],
        },
        fetch_index={
            "https://example.com/a": "Deep Research writes cited reports.",
            "https://example.com/b": "Deep Research tracks jury tally.",
            "https://example.com/c": RuntimeError("source unavailable"),
        },
    )


def _line(line_id: str, angle: str) -> dict[str, str]:
    """构造 search line，输入为 line_id/angle，输出为 planner line。"""
    return {
        "line_id": line_id,
        "topic": f"topic {line_id}",
        "query": f"query {line_id}",
        "why": f"cover {angle}",
        "angle": angle,
    }


def _fact(fact_id: str, source_id: str, statement: str, weight: str) -> dict[str, object]:
    """构造 fact，输入为 fact/source/statement/weight，输出为 fact dict。"""
    return {
        "fact_id": fact_id,
        "statement": statement,
        "excerpt": statement,
        "source_id": source_id,
        "source_url": f"https://example.com/{source_id.lower()}",
        "source_tier": "primary",
        "weight": weight,
        "confidence_hint": "high",
    }


def _group(group_id: str, statement: str, members: list[str]) -> dict[str, object]:
    """构造 fact group，输入为 ID/statement/members，输出为 group dict。"""
    return {
        "group_id": group_id,
        "canonical_statement": statement,
        "member_fact_ids": members,
        "source_ids": ["S-001"],
        "best_excerpt": statement,
        "support_count": len(members),
    }


def _jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件，输入为路径，输出为对象列表。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _task_log_paths_are_readable(workflow_dir: Path) -> bool:
    """验证每个 subagent.json 的 task_log_path 可读，输入为 workflow 目录，输出为布尔值。"""
    subagent_files = sorted((workflow_dir / "agents").glob("*/subagent.json"))
    if not subagent_files:
        return False
    for path in subagent_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        task_log_path = payload.get("task_log_path")
        if not isinstance(task_log_path, str) or not Path(task_log_path).is_file():
            return False
    return True
