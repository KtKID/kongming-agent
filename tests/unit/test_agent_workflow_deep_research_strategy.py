"""deep_research workflow strategy 单元测试。

本脚本验证 AgentWorkflowManager 的 deep_research 策略目录、describe 说明和确定性 fake 运行链路。
作用是固定 manager -> strategy -> source provider -> subagent -> artifact 的第一层集成合同。
关键执行流程：构造 fake source provider 和 subagent 哨兵，绑定到 workflow manager，运行 deep_research payload 后检查产物。
关键函数：_manager 构造 manager，_DeepResearchTaskExecutor 记录意外 child 调用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from application.agent_roles import AgentRoleManager
from application.agent_workflows.strategies.deep_research import FakeResearchSourceProvider
from infrastructure.config.models import Config, ModelSelectionConfig
from tests.support.workflow_strategy_manager import WorkflowStrategyTestManager


def test_agent_workflow_manager_registers_deep_research_strategy(tmp_path: Path) -> None:
    """验证策略目录，输入为默认 manager，输出为 deep_research catalog 和 describe 断言。"""
    manager = _manager(tmp_path, task_executor=object())

    catalog = manager.list_workflow_strategies()
    modes = [entry.mode for entry in catalog]
    assert "deep_research" in modes

    entry = next(item for item in catalog if item.mode == "deep_research")
    assert entry.status == "available"
    assert entry.runnable is True
    assert entry.title == "Deep Research 研究工作流"
    assert "引用" in entry.summary or "research" in entry.summary.lower()

    description = manager.describe_workflow_strategy("deep_research")
    assert description.runnable is True
    assert {field.name for field in description.inputs} >= {
        "topic",
        "objective",
        "limits",
    }
    assert any("report.md" in output for output in description.outputs)


@pytest.mark.asyncio
async def test_deep_research_strategy_runs_fake_provider_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    """验证 strategy 完整 fake 链路，输入为离线 provider/subagent，输出为 result 和 deep_research 产物。"""
    subagents = _DeepResearchTaskExecutor()
    manager = _manager(
        tmp_path,
        task_executor=subagents,
        source_provider=_source_provider(),
    )

    result = await manager.run_workflow_payload(
        mode="deep_research",
        parent_session_id="parent-session",
        payload=_payload(),
    )

    assert result.completed is True
    assert result.mode == "deep_research"
    assert subagents.tasks == []
    assert _phase_task_logs(result.workflow_dir) >= {
        "plan",
        "search",
        "extract",
        "group",
        "crosscheck",
        "report",
    }

    board_dir = result.workflow_dir / "deep_research"
    assert (result.workflow_dir / "result.json").is_file()
    assert (result.workflow_dir / "reports" / "index.json").is_file()
    assert (board_dir / "report.md").is_file()
    assert (board_dir / "stats.json").is_file()
    assert (result.workflow_dir / "audit.jsonl").is_file()

    root_result = json.loads((result.workflow_dir / "result.json").read_text(encoding="utf-8"))
    stats = json.loads((board_dir / "stats.json").read_text(encoding="utf-8"))
    checked = _jsonl(board_dir / "groups.checked.jsonl")
    audit_records = _jsonl(result.workflow_dir / "audit.jsonl")
    actions = {row["action"] for row in audit_records}
    checked_statuses = {row["ruling"]["status"] for row in checked}
    plan_task_log = _jsonl(result.workflow_dir / "agents" / "phase-plan" / "task.log.jsonl")
    phase_subagent = json.loads(
        (result.workflow_dir / "agents" / "phase-plan" / "subagent.json").read_text(
            encoding="utf-8"
        )
    )

    assert root_result["mode"] == "deep_research"
    assert root_result["completed"] is True
    report_path = Path(root_result["deep_research"]["artifact_paths"]["report_path"])
    assert report_path.parent.name == "deep_research"
    assert report_path.name == "report.md"
    assert stats["selected_source_count"] <= _payload()["limits"]["source_budget"]
    assert stats["top_fact_count"] <= _payload()["limits"]["fact_cap"]
    assert checked_statuses == {"upheld", "rejected"}
    assert "deep_research.workflow_started" in actions
    assert "deep_research.subagent_task_started" in actions
    assert "deep_research.subagent_task_completed" in actions
    assert "deep_research_completed" in actions
    assert all("subagent_runtime" not in row["payload"] for row in audit_records)
    assert all(
        row["payload"]["resolved_runtime"]["model"] == "gemma-4-e4b-it" for row in audit_records
    )
    assert "subagent_runtime" not in plan_task_log[0]
    assert plan_task_log[0]["resolved_runtime"]["model"] == "gemma-4-e4b-it"
    assert "subagent_runtime" not in phase_subagent
    assert phase_subagent["resolved_runtime"]["model"] == "gemma-4-e4b-it"


@pytest.mark.asyncio
async def test_deep_research_strategy_uses_current_manager_source_provider(
    tmp_path: Path,
) -> None:
    """验证 provider 热切换，输入为 setter 后的新 provider，输出为 result 使用新 provider。"""
    manager = _manager(
        tmp_path,
        task_executor=_DeepResearchTaskExecutor(),
        source_provider=_source_provider(name="provider-a"),
    )
    manager.set_deep_research_source_provider(_source_provider(name="provider-b"))

    result = await manager.run_workflow_payload(
        mode="deep_research",
        parent_session_id="parent-session",
        payload=_payload(),
    )

    assert result.data is not None
    assert result.data["deep_research"]["source_provider"] == "provider-b"


@pytest.mark.asyncio
async def test_deep_research_strategy_uses_payload_source_fixture(tmp_path: Path) -> None:
    """验证 payload fixture provider，输入为 source_fixture，输出为 fixture 来源产物。"""
    subagents = _DeepResearchTaskExecutor()
    manager = _manager(tmp_path, task_executor=subagents)

    result = await manager.run_workflow_payload(
        mode="deep_research",
        parent_session_id="parent-session",
        payload={
            "topic": "Fixture backed research",
            "source_queries": [
                {
                    "query_id": "fixture-q",
                    "line": "fixture source query",
                    "intent": "overview",
                    "max_results": 2,
                }
            ],
            "limits": {"source_budget": 2, "fetch_budget": 2, "fact_cap": 2},
            "source_fixture": {
                "name": "payload_fixture_provider",
                "search_index": {
                    "fixture-q": [
                        {"url": "https://fixture.example/report", "title": "Fixture Report"}
                    ]
                },
                "fetch_index": {
                    "https://fixture.example/report": "Fixture provider evidence enters the report."
                },
            },
        },
    )

    board_dir = result.workflow_dir / "deep_research"
    stats = json.loads((board_dir / "stats.json").read_text(encoding="utf-8"))
    report_markdown = (board_dir / "report.md").read_text(encoding="utf-8")

    assert result.data is not None
    assert result.data["deep_research"]["source_provider"] == "payload_fixture_provider"
    assert stats["selected_source_count"] == 1
    assert "Fixture provider evidence enters the report" in report_markdown
    assert subagents.tasks == []


class _DeepResearchTaskExecutor:
    """测试用 task executor，记录 fallback 链路里的意外 child 调用。"""

    def __init__(self) -> None:
        """初始化 fake manager，输入为空，输出为记录 task 的实例。"""
        self.tasks: list[Any] = []

    async def execute_task(
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
    task_executor: object,
    source_provider: FakeResearchSourceProvider | None = None,
) -> WorkflowStrategyTestManager:
    """构造 workflow manager，输入为临时目录和 task executor，输出为 manager。"""
    return WorkflowStrategyTestManager(
        task_executor=task_executor,  # type: ignore[arg-type]
        config=_config(tmp_path),
        workspace_root=tmp_path,
        role_manager=AgentRoleManager(role_dir=tmp_path / "roles"),
        deep_research_source_provider=source_provider,
    )


def _config(tmp_path: Path) -> Config:
    """构造测试配置，输入为临时目录，输出为 file session 配置。"""
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    cfg.session.file_store_path = str(tmp_path / "sessions")
    return cfg


def _payload() -> dict[str, Any]:
    """构造 deep_research payload，输入为空，输出为确定性测试参数。"""
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
                "line": "kongming deep research workflow architecture",
                "intent": "overview",
                "max_results": 3,
            },
            {
                "query_id": "L-002",
                "line": "kongming deep research validation",
                "intent": "skeptical",
                "max_results": 3,
            },
            {
                "query_id": "L-003",
                "line": "kongming deep research artifacts",
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
        "report": {
            "language": "zh-CN",
            "max_findings": 12,
            "include_rejected": True,
            "include_open_questions": True,
        },
    }


def _source_provider(*, name: str = "fake_research_source") -> FakeResearchSourceProvider:
    """构造 fake source provider，输入为空，输出为去重和读取 fixture。"""
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
        name=name,
    )


def _fact(fact_id: str, source_id: str, statement: str, weight: str) -> dict[str, object]:
    """构造 fact，输入为 ID/source/statement/weight，输出为 fact dict。"""
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


def _phase_task_logs(workflow_dir: Path) -> set[str]:
    """读取 phase task log，输入为 workflow 目录，输出为已完成 phase 集合。"""
    phases: set[str] = set()
    for path in sorted((workflow_dir / "agents").glob("phase-*/task.log.jsonl")):
        events = _jsonl(path)
        if events and events[-1]["event"] == "completed":
            phases.add(str(events[-1]["phase"]))
    return phases
