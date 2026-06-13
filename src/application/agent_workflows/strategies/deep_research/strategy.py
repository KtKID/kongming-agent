"""Deep Research workflow 策略状态机。

本脚本负责把 deep_research payload 转换为可运行的 Plan -> Search -> Extract -> Group -> Crosscheck -> Report 链路。
作用是让 AgentWorkflowStrategyManager 能以 mode=deep_research 分发研究任务，并产出公共 workflow 结果与 deep_research 细节产物。
关键执行流程：解析 DeepResearchSpec，写入 workflow manifest，调用 ResearchSourceManager 收集来源，确定性抽取事实和裁决，写入 FactBoard artifact、root result 和 audit。
关键类：DeepResearchStrategy 提供策略说明和运行入口，_DeterministicResearchSourceProvider 提供离线兜底来源。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from application.agent_workflows.context import WorkflowExecutionContext
from application.agent_workflows.strategies.deep_research.contracts import (
    CheckedFactGroup,
    DeepResearchSpec,
    FactGroup,
    JuryRuling,
    ResearchFactRecord,
    ResearchSourceCandidate,
    ResearchSourceProvider,
    ResearchSourceQuery,
    ResearchSourceRecord,
    parse_deep_research_spec,
)
from application.agent_workflows.strategies.deep_research.fact_board import (
    DeepResearchArtifactWriter,
)
from application.agent_workflows.strategies.deep_research.jury import (
    aggregate_jury_rulings,
)
from application.agent_workflows.strategies.deep_research.source_provider import (
    FakeResearchSourceProvider,
    ResearchSourceManager,
)
from application.agent_workflows.strategies.deep_research.task_log import (
    DeepResearchTaskLogWriter,
)
from application.agent_workflows.strategies.description import (
    WorkflowStrategyCatalogEntry,
    WorkflowStrategyDescription,
    WorkflowStrategyInputField,
)
from application.subagents.permissions import to_jsonable


class DeepResearchStrategy:
    """执行 deep_research 研究编排，组织来源收集、事实白板和确定性裁决。"""

    mode = "deep_research"

    def __init__(
        self,
        manager: Any,
        *,
        source_provider: ResearchSourceProvider | None = None,
    ) -> None:
        """初始化策略，输入为 AgentWorkflowManager 和可选来源 provider，输出为可注册策略实例。"""
        self._manager = manager
        self._source_provider = source_provider

    def catalog_entry(self) -> WorkflowStrategyCatalogEntry:
        """生成策略目录项，输入为当前策略说明，输出为父 agent 可查看的紧凑条目。"""
        return self.describe().catalog_entry()

    def describe(self) -> WorkflowStrategyDescription:
        """生成中文策略说明，输入为当前策略配置，输出为 payload 生成说明。"""
        return WorkflowStrategyDescription(
            mode=self.mode,
            title="Deep Research 研究工作流",
            status="available",
            runnable=True,
            summary="围绕研究问题规划搜索线、收集来源、抽取事实、分组交叉检查并生成带引用报告。",
            when_to_use=(
                "问题需要多来源证据和可追溯引用",
                "需要记录来源去重、预算裁剪、事实和裁决过程",
                "需要离线 fake provider 或确定性兜底跑通完整产物链路",
            ),
            warnings=(
                "v0.1 skeleton 使用确定性事实抽取和 fallback jury",
                "真实 WebSearch/WebFetch provider 后续通过 ResearchSourceProvider 接入",
                "报告质量取决于 provider 返回的正文质量和来源覆盖面",
            ),
            inputs=(
                WorkflowStrategyInputField(
                    name="topic",
                    required=True,
                    type_label="string",
                    description="用户研究问题。",
                    example="OpenAI Responses API tool calling 的官方当前用法是什么？",
                ),
                WorkflowStrategyInputField(
                    name="objective",
                    required=False,
                    type_label="string",
                    description="研究目标，省略时沿用 topic。",
                    example="整理官方文档中的工具调用主流程、限制和引用。",
                ),
                WorkflowStrategyInputField(
                    name="source_queries",
                    required=False,
                    type_label="array<object>",
                    description="显式搜索线，字段包含 query_id、line、intent、max_results。",
                    example=[
                        {
                            "query_id": "q1",
                            "line": "OpenAI Responses API tool calling official docs",
                            "intent": "primary_source",
                            "max_results": 3,
                        }
                    ],
                ),
                WorkflowStrategyInputField(
                    name="source_policy",
                    required=False,
                    type_label="object",
                    description="来源 provider 和检索偏好，v0.1 支持 fake/internal。",
                    example={
                        "provider": "fake",
                        "language": "zh-CN",
                        "allowed_domains": [],
                        "blocked_domains": [],
                        "prefer_primary_sources": True,
                    },
                ),
                WorkflowStrategyInputField(
                    name="limits",
                    required=False,
                    type_label="object",
                    description="来源预算、fetch 预算、fact_cap、jury_size 和 reject_quorum。",
                    example={
                        "source_budget": 6,
                        "fetch_budget": 4,
                        "fact_cap": 12,
                        "jury_size": 3,
                        "reject_quorum": 2,
                    },
                ),
            ),
            outputs=(
                "AgentWorkflowResult",
                "deep_research/plan.json",
                "deep_research/sources.jsonl",
                "deep_research/facts.jsonl",
                "deep_research/groups.jsonl",
                "deep_research/groups.checked.jsonl",
                "deep_research/rulings.jsonl",
                "deep_research/stats.json",
                "deep_research/report.md",
            ),
            examples=(
                {
                    "mode": "deep_research",
                    "payload": {
                        "topic": "OpenAI Responses API tool calling 的官方当前用法是什么？",
                        "limits": {
                            "source_budget": 3,
                            "fetch_budget": 3,
                            "fact_cap": 5,
                            "jury_size": 3,
                            "reject_quorum": 2,
                        },
                        "source_policy": {"provider": "fake"},
                    },
                },
            ),
        )

    async def run(
        self,
        context: WorkflowExecutionContext,
        payload: Mapping[str, object],
    ) -> Any:
        """执行 deep_research，输入为 workflow 上下文和 JSON payload，输出为 AgentWorkflowResult。"""
        spec = parse_deep_research_spec(payload)
        context.audit_writer.write_event(
            {
                "action": "deep_research.workflow_started",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "mode": self.mode,
                    "topic": spec.topic,
                    "output_contract": spec.output_contract,
                    "audit_tags": list(spec.audit_tags),
                },
            }
        )
        self._manager.write_workflow_manifest(context=context, tasks=[], status="running")

        artifact_writer = DeepResearchArtifactWriter(context.workflow_dir)
        task_log_writer = DeepResearchTaskLogWriter(
            workflow_dir=context.workflow_dir,
            audit_writer=context.audit_writer,
        )
        phase_summaries: list[dict[str, object]] = []

        plan_log = _start_phase_task(
            task_log_writer,
            phase="plan",
            input_artifacts=(),
            spec=spec,
        )
        plan_path = _write_plan(artifact_writer.root, context=context, spec=spec)
        _complete_phase_task(
            task_log_writer,
            phase="plan",
            task_run_id=plan_log,
            output_artifacts=(str(plan_path),),
            summaries=phase_summaries,
            metadata={"query_count": len(spec.source_queries)},
        )

        provider = self._resolve_source_provider(spec)
        source_manager = ResearchSourceManager(
            provider,
            audit_writer=context.audit_writer,
            max_content_chars=spec.limits.max_content_chars,
        )
        search_log = _start_phase_task(
            task_log_writer,
            phase="search",
            input_artifacts=(str(plan_path),),
            spec=spec,
            metadata={"provider_name": provider.name},
        )
        source_records = await source_manager.collect_sources(
            spec.source_queries,
            source_budget=spec.limits.source_budget,
            fetch_budget=spec.limits.fetch_budget,
        )
        sources_path = artifact_writer.write_sources(source_records)
        selected_sources_path = _write_selected_sources(artifact_writer.root, source_records)
        _complete_phase_task(
            task_log_writer,
            phase="search",
            task_run_id=search_log,
            output_artifacts=(str(sources_path), str(selected_sources_path)),
            summaries=phase_summaries,
            metadata={
                "provider_name": provider.name,
                "source_record_count": len(source_records),
            },
        )

        extract_log = _start_phase_task(
            task_log_writer,
            phase="extract",
            input_artifacts=(str(sources_path),),
            spec=spec,
        )
        facts = _extract_facts(source_records, fact_cap=spec.limits.fact_cap)
        facts_path = artifact_writer.write_facts(facts)
        _complete_phase_task(
            task_log_writer,
            phase="extract",
            task_run_id=extract_log,
            output_artifacts=(str(facts_path),),
            summaries=phase_summaries,
            metadata={"fact_count": len(facts)},
        )

        group_log = _start_phase_task(
            task_log_writer,
            phase="group",
            input_artifacts=(str(facts_path),),
            spec=spec,
        )
        groups = _group_facts(facts)
        groups_path = artifact_writer.write_groups(groups)
        _complete_phase_task(
            task_log_writer,
            phase="group",
            task_run_id=group_log,
            output_artifacts=(str(groups_path),),
            summaries=phase_summaries,
            metadata={"group_count": len(groups)},
        )

        crosscheck_log = _start_phase_task(
            task_log_writer,
            phase="crosscheck",
            input_artifacts=(str(groups_path),),
            spec=spec,
        )
        rulings = _rule_groups(groups, facts=facts, spec=spec)
        rulings_path = artifact_writer.write_rulings(rulings)
        checked_groups_path = _write_checked_groups(artifact_writer.root, groups, rulings)
        _complete_phase_task(
            task_log_writer,
            phase="crosscheck",
            task_run_id=crosscheck_log,
            output_artifacts=(str(rulings_path), str(checked_groups_path)),
            summaries=phase_summaries,
            metadata={
                "upheld_group_count": sum(1 for ruling in rulings if ruling.status == "upheld"),
                "rejected_group_count": sum(1 for ruling in rulings if ruling.status == "rejected"),
            },
        )

        report_log = _start_phase_task(
            task_log_writer,
            phase="report",
            input_artifacts=(str(checked_groups_path),),
            spec=spec,
        )
        report = _build_report(spec=spec, sources=source_records, facts=facts, rulings=rulings)
        report_path = artifact_writer.write_report(report)
        stats = _build_stats(
            spec=spec,
            source_records=source_records,
            facts=facts,
            groups=groups,
            rulings=rulings,
            provider_name=provider.name,
        )
        stats_path = artifact_writer.write_stats(stats)
        phase_summaries_path = _write_phase_summaries(artifact_writer.root, phase_summaries)
        _complete_phase_task(
            task_log_writer,
            phase="report",
            task_run_id=report_log,
            output_artifacts=(str(report_path), str(stats_path), str(phase_summaries_path)),
            summaries=phase_summaries,
            metadata={"report_path": str(report_path)},
        )

        finished_at = _now_iso()
        report_index_path = self._manager.write_report_index(
            context=context,
            status="completed",
            reports=(),
        )
        artifact_paths = {
            **artifact_writer.artifact_paths(),
            "plan_path": str(plan_path),
            "selected_sources_path": str(selected_sources_path),
            "checked_groups_path": str(checked_groups_path),
            "phase_summaries_path": str(phase_summaries_path),
        }
        extra = {
            "deep_research": {
                "topic": spec.topic,
                "objective": spec.objective,
                "source_provider": provider.name,
                "artifact_paths": artifact_paths,
                "stats": stats,
                "report_path": str(report_path),
                "phase_summaries": phase_summaries,
            }
        }
        self._manager.write_workflow_result(
            context=context,
            finished_at=finished_at,
            completed=True,
            report_index_path=report_index_path,
            reports=(),
            runs=(),
            extra=extra,
        )
        self._manager.write_workflow_manifest(
            context=context,
            tasks=[],
            status="completed",
            finished_at=finished_at,
        )
        context.audit_writer.write_event(
            {
                "action": "deep_research_completed",
                "payload": {
                    "workflow_id": context.workflow_id,
                    "mode": self.mode,
                    "completed": True,
                    "report_path": str(report_path),
                    "stats_path": str(stats_path),
                    "report_index_path": str(report_index_path),
                },
            }
        )

        from application.agent_workflows.manager import AgentWorkflowResult

        return AgentWorkflowResult(
            workflow_id=context.workflow_id,
            mode=self.mode,
            parent_session_id=context.parent_session_id,
            workflow_dir=context.workflow_dir,
            started_at=context.started_at,
            finished_at=finished_at,
            runs=(),
            reports=(),
            report_index_path=report_index_path,
            desc=context.desc,
            data=extra,
            completed_override=True,
        )

    def _resolve_source_provider(self, spec: DeepResearchSpec) -> ResearchSourceProvider:
        """解析来源 provider，输入为运行规格，输出为 fake、注入 provider 或确定性 provider。"""
        manager_provider = getattr(self._manager, "deep_research_source_provider", None)
        if manager_provider is not None:
            return cast(ResearchSourceProvider, manager_provider)
        if self._source_provider is not None:
            return self._source_provider
        fixture_provider = _provider_from_fixture(spec.source_fixture)
        if fixture_provider is not None:
            return fixture_provider
        return _DeterministicResearchSourceProvider(topic=spec.topic, objective=spec.objective)


class _DeterministicResearchSourceProvider:
    """确定性离线来源 provider，给无外部工具环境提供可复跑 fallback。"""

    name = "deterministic_research_source"

    def __init__(self, *, topic: str, objective: str) -> None:
        """初始化 provider，输入为研究主题和目标，输出为可搜索和读取的本地 provider。"""
        self._topic = topic
        self._objective = objective

    async def search(self, query: ResearchSourceQuery) -> tuple[ResearchSourceCandidate, ...]:
        """生成确定性候选，输入为搜索线，输出为单条本地候选。"""
        slug = _stable_slug(f"{query.query_id}-{query.line}")
        return (
            ResearchSourceCandidate(
                source_id="",
                query_id=query.query_id,
                url=f"https://kongming.local/deep-research/{slug}",
                canonical_url="",
                title=f"Deterministic source for {query.line}",
                snippet=f"Offline fallback evidence for {self._topic}",
                rank=1,
                provider_name=self.name,
            ),
        )

    async def fetch(self, candidate: ResearchSourceCandidate) -> ResearchSourceRecord:
        """读取确定性候选，输入为候选来源，输出为强来源记录。"""
        content = "\n".join(
            [
                f"Topic: {self._topic}",
                f"Objective: {self._objective}",
                f"Search line: {candidate.title}",
                "This deterministic fallback records the research boundary and allows the workflow artifact chain to be tested offline.",
            ]
        )
        return ResearchSourceRecord(
            source_id=candidate.source_id,
            query_id=candidate.query_id,
            url=candidate.url,
            canonical_url=candidate.canonical_url,
            title=candidate.title,
            status="fetched",
            tier="strong",
            content_text=content,
            error_code=None,
            error_message=None,
            provider_name=self.name,
            rank=candidate.rank,
        )


def _provider_from_fixture(fixture: Mapping[str, object]) -> ResearchSourceProvider | None:
    """从 payload fixture 构造 fake provider，输入为 source_fixture，输出为 provider 或 None。"""
    search_index = fixture.get("search_index")
    fetch_index = fixture.get("fetch_index")
    if not isinstance(search_index, Mapping) or not isinstance(fetch_index, Mapping):
        return None
    name = fixture.get("name")
    return FakeResearchSourceProvider(
        search_index=cast(Any, search_index),
        fetch_index=cast(Any, fetch_index),
        name=name.strip() if isinstance(name, str) and name.strip() else "fake_research_source",
    )


def _write_plan(root: Path, *, context: WorkflowExecutionContext, spec: DeepResearchSpec) -> Path:
    """写入计划 JSON，输入为 artifact 根目录和 spec，输出为 plan.json 路径。"""
    path = root / "plan.json"
    _write_json(
        path,
        {
            "workflow_id": context.workflow_id,
            "topic": spec.topic,
            "objective": spec.objective,
            "output_contract": spec.output_contract,
            "source_queries": [to_jsonable(query) for query in spec.source_queries],
            "limits": to_jsonable(spec.limits),
            "source_policy": to_jsonable(spec.source_policy),
            "audit_tags": list(spec.audit_tags),
            "written_at": _now_iso(),
        },
    )
    return path


def _write_selected_sources(root: Path, records: Sequence[ResearchSourceRecord]) -> Path:
    """写入 selected sources JSONL，输入为全部来源记录，输出为 selected 文件路径。"""
    selected = [record for record in records if record.status != "duplicate"]
    path = root / "sources.selected.jsonl"
    _write_jsonl(path, selected)
    return path


def _extract_facts(
    records: Sequence[ResearchSourceRecord],
    *,
    fact_cap: int,
) -> tuple[ResearchFactRecord, ...]:
    """从来源确定性抽取事实，输入为来源记录和 fact_cap，输出为事实记录。"""
    facts: list[ResearchFactRecord] = []
    for record in records:
        if record.status == "duplicate":
            continue
        statement = _statement_from_source(record)
        if not statement:
            continue
        facts.append(
            ResearchFactRecord(
                fact_id=f"fact-{len(facts) + 1:03d}",
                source_id=record.source_id,
                statement=statement,
                citation=f"{record.title or record.url} | {record.url}",
            )
        )
        if len(facts) >= fact_cap:
            break
    return tuple(facts)


def _group_facts(facts: Sequence[ResearchFactRecord]) -> tuple[FactGroup, ...]:
    """按事实逐条构造事实组，输入为事实序列，输出为事实组序列。"""
    return tuple(
        FactGroup(
            group_id=f"group-{index:03d}",
            canonical_statement=fact.statement,
            member_fact_ids=(fact.fact_id,),
            source_ids=(fact.source_id,),
            best_excerpt=fact.statement,
            support_count=1,
        )
        for index, fact in enumerate(facts, 1)
    )


def _rule_groups(
    groups: Sequence[FactGroup],
    *,
    facts: Sequence[ResearchFactRecord],
    spec: DeepResearchSpec,
) -> tuple[CheckedFactGroup, ...]:
    """确定性裁决事实组，输入为事实组、事实和 spec，输出为 JuryRuling 序列。"""
    fact_by_id = {fact.fact_id: fact for fact in facts}
    rulings = []
    for group in groups:
        group_facts = [
            fact
            for fact_id in group.member_fact_ids
            if (fact := fact_by_id.get(fact_id)) is not None
        ]
        decision = (
            "reject" if any("unavailable" in fact.statement for fact in group_facts) else "uphold"
        )
        votes = tuple(
            JuryRuling(
                ruling_id=f"ruling-{group.group_id}-{index:03d}",
                group_id=group.group_id,
                juror_id=f"fallback-juror-{index:03d}",
                reject=decision == "reject",
                abstain=False,
                reason="deterministic fallback",
                contradicting_evidence=(),
                source_coverage="covered",
            )
            for index in range(1, spec.limits.jury_size + 1)
        )
        rulings.append(aggregate_jury_rulings(group=group, rulings=votes, limits=spec.limits))
    return tuple(rulings)


def _write_checked_groups(
    root: Path,
    groups: Sequence[FactGroup],
    rulings: Sequence[CheckedFactGroup],
) -> Path:
    """写入裁决后的事实组，输入为 groups 和 rulings，输出为 groups.checked.jsonl 路径。"""
    ruling_by_group = {ruling.group_id: ruling for ruling in rulings}
    rows = []
    for group in groups:
        ruling = ruling_by_group.get(group.group_id)
        rows.append(
            {
                "group": to_jsonable(group),
                "ruling": to_jsonable(ruling) if ruling is not None else None,
            }
        )
    path = root / "groups.checked.jsonl"
    _write_jsonl(path, rows)
    return path


def _build_report(
    *,
    spec: DeepResearchSpec,
    sources: Sequence[ResearchSourceRecord],
    facts: Sequence[ResearchFactRecord],
    rulings: Sequence[CheckedFactGroup],
) -> str:
    """构造 Markdown 报告，输入为 spec、来源、事实和裁决，输出为 report.md 内容。"""
    upheld = [ruling for ruling in rulings if ruling.status == "upheld"]
    rejected = [ruling for ruling in rulings if ruling.status == "rejected"]
    source_by_id = {source.source_id: source for source in sources}
    lines = [
        f"# {spec.topic}",
        "",
        f"Objective: {spec.objective}",
        "",
        "## Tally",
        "",
        f"- Upheld groups: {len(upheld)}",
        f"- Rejected groups: {len(rejected)}",
        f"- Total facts: {len(facts)}",
        "",
        "## Findings",
        "",
    ]
    if not rulings:
        lines.append("- No facts were extracted from the selected sources.")
    for ruling in rulings:
        group_facts = [
            fact for fact in facts if fact.fact_id in _fact_ids_for_group(ruling.group_id, facts)
        ]
        if not group_facts and facts:
            index = _group_index(ruling.group_id)
            if index is not None and 0 <= index - 1 < len(facts):
                group_facts = [facts[index - 1]]
        statement = group_facts[0].statement if group_facts else ruling.group_id
        citation = group_facts[0].citation if group_facts else ""
        lines.append(f"- {ruling.status}: {statement}")
        lines.append(f"  - tally: {ruling.tally}; reason: {ruling.decision_reason}")
        if citation:
            lines.append(f"  - citation: {citation}")
    lines.extend(["", "## Sources", ""])
    for index, source in enumerate(sources, 1):
        status = f"{source.status}/{source.tier}"
        lines.append(f"[{index}] {source.title or source.url} - {source.url} ({status})")
    lines.extend(["", "## Citations", ""])
    for index, fact in enumerate(facts, 1):
        source_ref = source_by_id.get(fact.source_id)
        url = source_ref.url if source_ref is not None else fact.citation
        lines.append(f"[F{index}] {fact.statement} - {url}")
    return "\n".join(lines).strip() + "\n"


def _fact_ids_for_group(group_id: str, facts: Sequence[ResearchFactRecord]) -> tuple[str, ...]:
    """按 deterministic group_id 推断事实 ID，输入为 group_id 和事实列表，输出为 fact_id 元组。"""
    index = _group_index(group_id)
    if index is None or index < 1 or index > len(facts):
        return ()
    return (facts[index - 1].fact_id,)


def _group_index(group_id: str) -> int | None:
    """读取 group 序号，输入为 group_id，输出为整数或 None。"""
    prefix = "group-"
    if not group_id.startswith(prefix):
        return None
    suffix = group_id[len(prefix) :]
    return int(suffix) if suffix.isdigit() else None


def _build_stats(
    *,
    spec: DeepResearchSpec,
    source_records: Sequence[ResearchSourceRecord],
    facts: Sequence[ResearchFactRecord],
    groups: Sequence[FactGroup],
    rulings: Sequence[CheckedFactGroup],
    provider_name: str,
) -> dict[str, object]:
    """构造统计数据，输入为阶段产物，输出为 stats.json payload。"""
    return {
        "topic": spec.topic,
        "source_provider": provider_name,
        "source_budget": spec.limits.source_budget,
        "fetch_budget": spec.limits.fetch_budget,
        "fact_cap": spec.limits.fact_cap,
        "source_record_count": len(source_records),
        "selected_source_count": sum(
            1 for record in source_records if record.status != "duplicate"
        ),
        "fetched_source_count": sum(1 for record in source_records if record.status == "fetched"),
        "failed_source_count": sum(1 for record in source_records if record.status == "failed"),
        "duplicate_source_count": sum(
            1 for record in source_records if record.status == "duplicate"
        ),
        "raw_fact_count": len(facts),
        "top_fact_count": len(facts),
        "group_count": len(groups),
        "upheld_group_count": sum(1 for ruling in rulings if ruling.status == "upheld"),
        "rejected_group_count": sum(1 for ruling in rulings if ruling.status == "rejected"),
        "written_at": _now_iso(),
    }


def _start_phase_task(
    writer: DeepResearchTaskLogWriter,
    *,
    phase: str,
    input_artifacts: Sequence[str],
    spec: DeepResearchSpec,
    metadata: Mapping[str, object] | None = None,
) -> str:
    """记录阶段开始，输入为 task log writer 和阶段信息，输出为 task_run_id。"""
    task_run_id = f"phase-{phase}"
    writer.start_task(
        task_run_id=task_run_id,
        phase=phase,
        role=f"{phase}_fallback",
        input_artifacts=input_artifacts,
        prompt_hash=_prompt_hash(phase, spec),
        tool_allowlist=(),
        budget_snapshot=to_jsonable(spec.limits),
        metadata=metadata,
    )
    return task_run_id


def _complete_phase_task(
    writer: DeepResearchTaskLogWriter,
    *,
    phase: str,
    task_run_id: str,
    output_artifacts: Sequence[str],
    summaries: list[dict[str, object]],
    metadata: Mapping[str, object] | None = None,
) -> None:
    """记录阶段完成，输入为阶段产物和 metadata，输出为 task log 与 summaries 更新。"""
    writer.complete_task(
        task_run_id=task_run_id,
        phase=phase,
        role=f"{phase}_fallback",
        output_artifacts=output_artifacts,
        status="completed",
        metadata=metadata,
    )
    summaries.append(
        {
            "phase": phase,
            "task_run_id": task_run_id,
            "status": "completed",
            "output_artifacts": list(output_artifacts),
            "metadata": dict(metadata or {}),
            "completed_at": _now_iso(),
        }
    )


def _write_phase_summaries(root: Path, summaries: Sequence[Mapping[str, object]]) -> Path:
    """写入阶段摘要，输入为摘要序列，输出为 phase_summaries.json 路径。"""
    path = root / "phase_summaries.json"
    _write_json(path, {"phases": list(summaries), "written_at": _now_iso()})
    return path


def _statement_from_source(record: ResearchSourceRecord) -> str:
    """从来源生成事实陈述，输入为来源记录，输出为一句事实文本。"""
    if record.status == "fetched" and record.content_text:
        return _first_sentence(record.content_text)
    if record.status in {"failed", "candidate"}:
        reason = record.error_message or record.error_code or "source unavailable"
        return f"Source unavailable: {record.title or record.url}; reason={reason}"
    return ""


def _first_sentence(value: str) -> str:
    """提取正文首句，输入为正文文本，输出为简短陈述。"""
    compact = " ".join(value.split())
    if not compact:
        return ""
    for delimiter in (". ", "。", "\n"):
        if delimiter in compact:
            return compact.split(delimiter, 1)[0].strip()[:500]
    return compact[:500]


def _prompt_hash(phase: str, spec: DeepResearchSpec) -> str:
    """生成阶段 prompt hash，输入为 phase 和 spec，输出为短 SHA256。"""
    payload = f"{phase}\n{spec.topic}\n{spec.objective}\n{spec.output_contract}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _stable_slug(value: str) -> str:
    """生成稳定 URL slug，输入为字符串，输出为短 hash。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    """原子写入 JSON，输入为路径和 payload，输出为文件更新。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)


def _write_jsonl(path: Path, records: Sequence[object]) -> None:
    """写入 JSONL，输入为路径和记录序列，输出为目标文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(to_jsonable(record), ensure_ascii=False, default=str))
            handle.write("\n")


def _now_iso() -> str:
    """生成当前 UTC 时间，输入为空，输出为 ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


__all__ = ["DeepResearchStrategy"]
