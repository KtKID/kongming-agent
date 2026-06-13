"""Deep Research 来源数据合同。

本脚本定义来源检索、来源候选、来源读取结果和 provider protocol。
作用是让 DeepResearchStrategy、source manager、fake provider 和测试使用同一组结构化字段。
关键执行流程：planner 产出 ResearchSourceQuery，provider 返回 ResearchSourceCandidate，manager 归一化为 ResearchSourceRecord。
关键函数：ResearchSourceProvider.search 检索候选来源，ResearchSourceProvider.fetch 读取候选来源。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

SourceStatus = Literal["candidate", "selected", "fetched", "skipped", "failed", "duplicate"]
SourceTier = Literal["primary", "secondary", "blog", "forum", "weak", "strong", "duplicate"]
SourceFit = Literal["high", "medium", "low"]
FactStatus = Literal["pending", "upheld", "rejected"]
JuryDecision = Literal["uphold", "reject", "abstain"]
FactWeight = Literal["key", "support", "aside"]
PhaseStatus = Literal["pending", "running", "completed", "degraded", "failed", "skipped"]
DEFAULT_OUTPUT_CONTRACT = "deep_research_report"


class DeepResearchContractError(ValueError):
    """Deep Research payload 合同错误。"""


@dataclass(frozen=True)
class ResearchSourceQuery:
    """单条研究搜索线，输入为 query id 和查询文本，输出为 provider 的 search 参数。"""

    # 搜索线稳定 ID。
    query_id: str
    # 实际搜索文本。
    line: str
    # 搜索意图，例如 overview、technical、skeptical。
    intent: str
    # 当前搜索线最多返回候选数。
    max_results: int


@dataclass(frozen=True)
class ResearchSourceCandidate:
    """来源候选，输入来自 search 结果，输出给 dedupe 和 fetch 阶段。"""

    # 候选来源 ID，provider 可留空，由 manager 稳定补齐。
    source_id: str
    # 所属搜索线 ID。
    query_id: str
    # 原始 URL。
    url: str
    # 规范化 URL，provider 可留空，由 manager 统一计算。
    canonical_url: str
    # 页面标题。
    title: str
    # 搜索摘要。
    snippet: str
    # 搜索线内排名，从 1 开始。
    rank: int
    # provider 名称。
    provider_name: str


@dataclass(frozen=True)
class ResearchSourceRecord:
    """来源读取记录，输入来自 fetch 或降级路径，输出给后续 Extract 阶段。"""

    # 稳定来源 ID。
    source_id: str
    # 所属搜索线 ID。
    query_id: str
    # 原始 URL。
    url: str
    # 规范化 URL。
    canonical_url: str
    # 页面标题。
    title: str
    # 当前来源状态。
    status: SourceStatus
    # 来源等级，strong 表示可读取正文，weak 表示降级，duplicate 表示被折叠。
    tier: SourceTier
    # 正文文本；失败、重复或只保留候选时为空。
    content_text: str | None
    # 失败代码；成功和重复时为空。
    error_code: str | None
    # 失败摘要；成功和重复时为空。
    error_message: str | None
    # provider 名称。
    provider_name: str
    # 搜索线内排名。
    rank: int
    # 重复来源指向保留的 source_id。
    duplicate_of: str | None = None


class ResearchSourceProvider(Protocol):
    """来源 provider 协议，约束 search 和 fetch 两个异步入口。"""

    name: str

    async def search(self, query: ResearchSourceQuery) -> tuple[ResearchSourceCandidate, ...]:
        """检索候选来源，输入为搜索线，输出为候选来源列表。"""
        ...

    async def fetch(self, candidate: ResearchSourceCandidate) -> ResearchSourceRecord:
        """读取候选来源，输入为候选来源，输出为结构化来源记录。"""
        ...


@dataclass(frozen=True)
class DeepResearchLimits:
    """Deep Research 预算限制，输入来自 payload.limits，输出给各阶段使用。"""

    # 最多保留唯一来源数量。
    source_budget: int = 10
    # 最多 fetch 来源数量。
    fetch_budget: int = 10
    # 最多保留事实数量。
    fact_cap: int = 20
    # 每个事实组的陪审员数量。
    jury_size: int = 3
    # 否决事实所需 reject 票数。
    reject_quorum: int = 2
    # 单来源正文最大字符数。
    max_content_chars: int = 60000
    # 每条搜索线默认候选数。
    search_results_per_line: int = 6
    # fetch 并发数。
    fetch_concurrency: int = 4
    # jury 并发数。
    jury_concurrency: int = 6
    # workflow 总超时秒数。
    workflow_timeout_seconds: int = 2400


@dataclass(frozen=True)
class DeepResearchSourcePolicy:
    """来源策略，输入来自 payload.source_policy，输出给 strategy 选择 provider。"""

    # 来源语言偏好。
    language: str = "zh-CN"
    # 新鲜度天数，None 表示不限制。
    freshness_days: int | None = None
    # 允许域名。
    allowed_domains: tuple[str, ...] = ()
    # 阻止域名。
    blocked_domains: tuple[str, ...] = ()
    # 是否优先一手来源。
    prefer_primary_sources: bool = True
    # 当前支持 fake/internal，真实 provider 后续通过该字段扩展。
    provider: str = "fake"


@dataclass(frozen=True)
class DeepResearchReportOptions:
    """报告选项，输入来自 payload.report，输出给 Report 阶段。"""

    # 报告语言。
    language: str = "zh-CN"
    # 最多保留发现数量。
    max_findings: int = 12
    # 是否包含被否决事实。
    include_rejected: bool = True
    # 是否包含开放问题。
    include_open_questions: bool = True


@dataclass(frozen=True)
class DeepResearchSpec:
    """Deep Research 运行规格，输入为 tool payload，输出为 strategy 的单一合同。"""

    topic: str
    objective: str
    source_queries: tuple[ResearchSourceQuery, ...]
    limits: DeepResearchLimits
    source_policy: DeepResearchSourcePolicy
    output_contract: str
    source_fixture: Mapping[str, object]
    audit_tags: tuple[str, ...]
    report: DeepResearchReportOptions = field(default_factory=DeepResearchReportOptions)
    mode: Literal["deep_research"] = "deep_research"


@dataclass(frozen=True)
class SearchLine:
    """搜索线合同，输入来自 planner，输出给 Search 阶段。"""

    # 稳定搜索线 ID。
    line_id: str
    # 搜索主题。
    topic: str
    # 实际查询。
    query: str
    # 搜索理由。
    why: str
    # 搜索角度。
    angle: str


@dataclass(frozen=True)
class ResearchPlan:
    """研究计划合同，输入来自 planner，输出到 plan.json。"""

    # 用户研究问题。
    topic: str
    # 研究目标。
    objective: str
    # 搜索线集合。
    lines: tuple[SearchLine, ...]
    # planner task run id。
    created_by: str


@dataclass(frozen=True)
class SearchHit:
    """搜索结果合同，输入来自 Search 阶段，输出到 search_hits.jsonl。"""

    # 稳定 hit ID。
    hit_id: str
    # 所属搜索线。
    line_id: str
    # 原始 URL。
    url: str
    # 规范化 URL。
    canonical_url: str
    # 标题。
    title: str
    # 摘要。
    snippet: str
    # 匹配度。
    fit: SourceFit
    # 搜索线内排名。
    rank: int


@dataclass(frozen=True)
class ResearchFactRecord:
    """可证伪事实记录，输入来自 Extract 阶段，输出给 Group 和 Crosscheck。"""

    fact_id: str
    source_id: str
    statement: str
    citation: str
    status: FactStatus = "pending"


@dataclass(frozen=True)
class ResearchFactGroup:
    """事实等价组，输入为事实集合，输出给 jury 裁决。"""

    group_id: str
    fact_ids: tuple[str, ...]
    statement: str


@dataclass(frozen=True)
class FactGroup:
    """文档 spec 中的事实组合同，输入为等价事实，输出给 jury 聚合。"""

    group_id: str
    canonical_statement: str
    member_fact_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    best_excerpt: str
    support_count: int


@dataclass(frozen=True)
class ExtractedFact:
    """文档 spec 中的事实合同，输入来自 Extract 阶段，输出给 FactBoard。"""

    # 稳定 fact ID。
    fact_id: str
    # 可证伪断言。
    statement: str
    # 原文引用片段。
    excerpt: str
    # 来源 ID。
    source_id: str
    # 来源 URL。
    source_url: str
    # 来源等级。
    source_tier: str
    # 事实权重。
    weight: FactWeight
    # extractor 初步信心。
    confidence_hint: str


@dataclass(frozen=True)
class JuryVote:
    """单个陪审票，输入为 juror 输出，输出给 AdversarialJury 聚合。"""

    juror_id: str
    decision: JuryDecision
    reason: str


@dataclass(frozen=True)
class JuryRuling:
    """单个 juror 裁决，输入来自 Crosscheck 子任务，输出给聚合器。"""

    ruling_id: str
    group_id: str
    juror_id: str
    reject: bool
    abstain: bool
    reason: str
    contradicting_evidence: tuple[str, ...] = ()
    source_coverage: str = ""


@dataclass(frozen=True)
class CheckedFactGroup:
    """事实组聚合裁决，输入为 jury rulings，输出给 Report 阶段。"""

    group_id: str
    status: FactStatus
    cast_count: int
    reject_count: int
    abstain_count: int
    tally: str
    decision_reason: str


@dataclass(frozen=True)
class ResearchStats:
    """运行统计合同，输入来自各阶段计数，输出到 stats.json。"""

    # 搜索线数量。
    search_line_count: int = 0
    # 原始搜索结果数量。
    raw_hit_count: int = 0
    # 选中来源数量。
    selected_source_count: int = 0
    # 重复来源数量。
    duplicate_source_count: int = 0
    # 预算溢出来源数量。
    overflow_source_count: int = 0
    # fetched 来源数量。
    fetched_source_count: int = 0
    # 原始事实数量。
    raw_fact_count: int = 0
    # 截断后事实数量。
    top_fact_count: int = 0
    # 事实组数量。
    group_count: int = 0
    # jury 任务数量。
    jury_task_count: int = 0
    # 弃权数量。
    abstain_count: int = 0
    # 存活事实组数量。
    upheld_count: int = 0
    # 被否决事实组数量。
    rejected_count: int = 0
    # 报告是否使用 fallback。
    report_fallback: bool = False


@dataclass(frozen=True)
class ResearchReport:
    """最终报告合同，输入来自 Report 阶段，输出到 report.json/report.md。"""

    # 核心回答。
    answer: str
    # 存活发现。
    findings: tuple[Mapping[str, Any], ...]
    # 被否决事实。
    rejected: tuple[Mapping[str, Any], ...]
    # 局限性。
    limitations: tuple[str, ...]
    # 开放问题。
    open_questions: tuple[str, ...]
    # 统计信息。
    stats: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PhaseSummary:
    """阶段摘要合同，输入来自阶段完成点，输出到 phase_summaries.jsonl。"""

    # 阶段名。
    phase: str
    # 阶段状态。
    status: PhaseStatus
    # artifact 相对路径。
    artifact_paths: tuple[str, ...]
    # 本阶段统计增量。
    stats_delta: Mapping[str, Any] = field(default_factory=dict)
    # 降级或失败原因。
    reason: str | None = None


class DeepResearchContractParser:
    """Deep Research payload parser 门面。"""

    def parse(self, payload: Mapping[str, object]) -> DeepResearchSpec:
        """解析 payload，输入为原始映射，输出为 DeepResearchSpec。"""
        return parse_deep_research_spec(payload)


def parse_deep_research_spec(payload: Mapping[str, object]) -> DeepResearchSpec:
    """解析运行规格，输入为 payload 映射，输出为 DeepResearchSpec。"""
    payload = _payload_mapping(payload)
    topic = _required_text(payload.get("topic"), "topic")
    if "objective" in payload and not _text(payload.get("objective")):
        raise DeepResearchContractError("deep_research payload objective must be non-empty")
    objective = _text(payload.get("objective")) or topic
    limits = _parse_limits(_object(payload.get("limits")))
    source_policy = _parse_source_policy(_object(payload.get("source_policy")))
    report = _parse_report_options(_object(payload.get("report")), source_policy=source_policy)
    source_queries = _parse_queries(
        payload.get("source_queries"),
        topic=topic,
        default_max_results=limits.search_results_per_line,
    )
    if "output_contract" in payload:
        raw_output_contract = payload.get("output_contract")
        if not isinstance(raw_output_contract, str) or not raw_output_contract.strip():
            raise DeepResearchContractError("output_contract must be deep_research_report")
        output_contract = raw_output_contract.strip()
    else:
        output_contract = DEFAULT_OUTPUT_CONTRACT
    if output_contract != DEFAULT_OUTPUT_CONTRACT:
        raise DeepResearchContractError("output_contract must be deep_research_report")
    source_fixture = _object(payload.get("source_fixture"))
    audit_tags = tuple(_string_array(payload.get("audit_tags")))
    return DeepResearchSpec(
        topic=topic,
        objective=objective,
        source_queries=source_queries,
        limits=limits,
        source_policy=source_policy,
        output_contract=output_contract,
        source_fixture=source_fixture,
        audit_tags=audit_tags,
        report=report,
    )


def _parse_limits(raw: Mapping[str, object]) -> DeepResearchLimits:
    """解析 limits，输入为原始映射，输出为带默认值的预算对象。"""
    limits = DeepResearchLimits(
        source_budget=_int(raw.get("source_budget"), default=10),
        fetch_budget=_int(raw.get("fetch_budget"), default=10),
        fact_cap=_int(raw.get("fact_cap"), default=20),
        jury_size=_int(raw.get("jury_size"), default=3),
        reject_quorum=_int(raw.get("reject_quorum"), default=2),
        max_content_chars=_int(raw.get("max_content_chars"), default=60000),
        search_results_per_line=_int(raw.get("search_results_per_line"), default=6),
        fetch_concurrency=_int(raw.get("fetch_concurrency"), default=4),
        jury_concurrency=_int(raw.get("jury_concurrency"), default=6),
        workflow_timeout_seconds=_int(raw.get("workflow_timeout_seconds"), default=2400),
    )
    if limits.source_budget <= 0:
        raise DeepResearchContractError("limits.source_budget must be between 1 and 15")
    if limits.source_budget > 15:
        raise DeepResearchContractError("limits.source_budget must be between 1 and 15")
    if limits.fetch_budget < 0:
        raise DeepResearchContractError("limits.fetch_budget must be >= 0")
    if limits.fact_cap <= 0:
        raise DeepResearchContractError("limits.fact_cap must be between 1 and 25")
    if limits.fact_cap > 25:
        raise DeepResearchContractError("limits.fact_cap must be between 1 and 25")
    if limits.jury_size <= 0:
        raise DeepResearchContractError("limits.jury_size must be > 0")
    if limits.reject_quorum <= 0:
        raise DeepResearchContractError("limits.reject_quorum must be > 0")
    if limits.reject_quorum > limits.jury_size:
        raise DeepResearchContractError("limits.reject_quorum must be <= limits.jury_size")
    if limits.max_content_chars <= 0:
        raise DeepResearchContractError("limits.max_content_chars must be > 0")
    if limits.search_results_per_line <= 0:
        raise DeepResearchContractError("limits.search_results_per_line must be > 0")
    if limits.fetch_concurrency <= 0:
        raise DeepResearchContractError("limits.fetch_concurrency must be > 0")
    if limits.jury_concurrency <= 0:
        raise DeepResearchContractError("limits.jury_concurrency must be > 0")
    if limits.workflow_timeout_seconds <= 0:
        raise DeepResearchContractError("limits.workflow_timeout_seconds must be > 0")
    return limits


def _parse_source_policy(raw: Mapping[str, object]) -> DeepResearchSourcePolicy:
    """解析来源策略，输入为原始映射，输出为来源策略对象。"""
    provider = _text(raw.get("provider")) or "fake"
    if provider not in {"fake", "internal"}:
        raise DeepResearchContractError("source_policy.provider must be fake or internal")
    return DeepResearchSourcePolicy(
        language=_text(raw.get("language")) or "zh-CN",
        freshness_days=_optional_int(raw.get("freshness_days")),
        allowed_domains=tuple(_string_array(raw.get("allowed_domains"))),
        blocked_domains=tuple(_string_array(raw.get("blocked_domains"))),
        prefer_primary_sources=_bool(raw.get("prefer_primary_sources"), default=True),
        provider=provider,
    )


def _parse_report_options(
    raw: Mapping[str, object],
    *,
    source_policy: DeepResearchSourcePolicy,
) -> DeepResearchReportOptions:
    """解析报告选项，输入为原始映射和来源策略，输出为报告选项。"""
    max_findings = _int(raw.get("max_findings"), default=12)
    if max_findings <= 0:
        raise DeepResearchContractError("report.max_findings must be > 0")
    return DeepResearchReportOptions(
        language=_text(raw.get("language")) or source_policy.language,
        max_findings=max_findings,
        include_rejected=_bool(raw.get("include_rejected"), default=True),
        include_open_questions=_bool(raw.get("include_open_questions"), default=True),
    )


def _parse_queries(
    value: object,
    *,
    topic: str,
    default_max_results: int,
) -> tuple[ResearchSourceQuery, ...]:
    """解析搜索线，输入为 payload 字段和 topic，输出为至少一条 query。"""
    if not isinstance(value, Sequence) or isinstance(value, str):
        return (
            ResearchSourceQuery(
                query_id="q1",
                line=topic,
                intent="overview",
                max_results=default_max_results,
            ),
        )
    queries: list[ResearchSourceQuery] = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, Mapping):
            continue
        line = _text(item.get("line")) or topic
        queries.append(
            ResearchSourceQuery(
                query_id=_text(item.get("query_id")) or f"q{index}",
                line=line,
                intent=_text(item.get("intent")) or "overview",
                max_results=max(0, _int(item.get("max_results"), default=default_max_results)),
            )
        )
    return tuple(queries) or (
        ResearchSourceQuery(
            query_id="q1",
            line=topic,
            intent="overview",
            max_results=default_max_results,
        ),
    )


def _required_text(value: object, field: str) -> str:
    """读取必填文本，输入为任意值和字段名，输出为非空字符串。"""
    text = _text(value)
    if not text:
        raise DeepResearchContractError(f"deep_research payload requires non-empty {field}")
    return text


def _payload_mapping(payload: Mapping[str, object]) -> Mapping[str, object]:
    """选择实际 payload，输入为 tool 参数或 payload 本体，输出为解析主体。"""
    nested = payload.get("payload")
    if "topic" not in payload and isinstance(nested, Mapping):
        return nested
    return payload


def _text(value: object) -> str:
    """读取文本字段，输入为任意值，输出为去空白字符串。"""
    return value.strip() if isinstance(value, str) else ""


def _object(value: object) -> Mapping[str, object]:
    """读取对象字段，输入为任意值，输出为映射或空映射。"""
    return value if isinstance(value, Mapping) else {}


def _int(value: object, *, default: int) -> int:
    """读取整数字段，输入为任意值，输出为 int 或默认值。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
            return int(stripped)
    return default


def _optional_int(value: object) -> int | None:
    """读取可选整数字段，输入为任意值，输出为 int 或 None。"""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _int(value, default=0)


def _bool(value: object, *, default: bool) -> bool:
    """读取布尔字段，输入为任意值，输出为 bool。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return default


def _string_array(value: object) -> list[str]:
    """读取字符串数组，输入为任意值，输出为字符串列表。"""
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


__all__ = [
    "CheckedFactGroup",
    "DeepResearchContractError",
    "DeepResearchContractParser",
    "DeepResearchLimits",
    "DeepResearchReportOptions",
    "DeepResearchSourcePolicy",
    "DeepResearchSpec",
    "ExtractedFact",
    "FactGroup",
    "FactWeight",
    "JuryDecision",
    "JuryRuling",
    "JuryVote",
    "PhaseStatus",
    "PhaseSummary",
    "ResearchFactGroup",
    "ResearchFactRecord",
    "ResearchPlan",
    "ResearchReport",
    "ResearchSourceCandidate",
    "ResearchSourceProvider",
    "ResearchSourceQuery",
    "ResearchSourceRecord",
    "ResearchStats",
    "SearchHit",
    "SearchLine",
    "SourceFit",
    "SourceStatus",
    "SourceTier",
    "parse_deep_research_spec",
]
