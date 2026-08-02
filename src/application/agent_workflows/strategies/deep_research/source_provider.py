"""Deep Research 来源收集 manager 和 fake provider。

本脚本实现来源检索与读取的第一层运行能力。
作用是通过 ResearchSourceManager 统一 provider.search、SourceDeduper、provider.fetch、失败降级和审计事件。
关键执行流程：collect_sources 收集所有 query 候选，按 canonical URL 去重和预算裁剪，再 fetch 选中来源并返回 ResearchSourceRecord。
关键函数：ResearchSourceManager.collect_sources 执行来源收集，FakeResearchSourceProvider.search/fetch 提供离线确定性替身。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from application.agent_workflows.strategies.deep_research.contracts import (
    ResearchSourceCandidate,
    ResearchSourceProvider,
    ResearchSourceQuery,
    ResearchSourceRecord,
    SourceStatus,
    SourceTier,
)
from application.agent_workflows.strategies.deep_research.dedupe import (
    DuplicateSource,
    SourceDeduper,
    normalize_candidate,
    stable_source_id,
)


class ResearchSourceManager:
    """来源检索与读取门面，给 DeepResearchStrategy 提供唯一入口。"""

    def __init__(
        self,
        provider: ResearchSourceProvider,
        *,
        audit_writer: Any | None = None,
        deduper: SourceDeduper | None = None,
        max_content_chars: int = 60000,
    ) -> None:
        """初始化 manager，输入为 provider 和可选审计 writer，输出为可收集来源的实例。"""
        if max_content_chars <= 0:
            raise ValueError("max_content_chars must be > 0")
        self._provider = provider
        self._audit_writer = audit_writer
        self._deduper = deduper or SourceDeduper()
        self._max_content_chars = max_content_chars

    async def collect_sources(
        self,
        queries: Sequence[ResearchSourceQuery],
        *,
        source_budget: int,
        fetch_budget: int,
    ) -> tuple[ResearchSourceRecord, ...]:
        """收集来源，输入为搜索线和预算，输出为 fetched/failed/candidate/duplicate 记录。"""
        if source_budget < 0:
            raise ValueError("source_budget must be >= 0")
        if fetch_budget < 0:
            raise ValueError("fetch_budget must be >= 0")

        candidates, search_failed_records = await self._search_all(queries)
        deduped = self._deduper.select(candidates, source_budget=source_budget)
        for selected in deduped.selected:
            self._write_audit(
                "deep_research.source_selected",
                {
                    "source_id": selected.source_id,
                    "query_id": selected.query_id,
                    "canonical_url": selected.canonical_url,
                    "url": selected.url,
                    "rank": selected.rank,
                    "provider_name": selected.provider_name,
                },
            )
        for duplicate in deduped.duplicates:
            self._write_duplicate_audit(duplicate)
        for overflow in deduped.overflow:
            self._write_audit(
                "deep_research.source_overflow",
                {
                    "source_id": overflow.source_id,
                    "query_id": overflow.query_id,
                    "canonical_url": overflow.canonical_url,
                    "source_budget": source_budget,
                    "provider_name": overflow.provider_name,
                },
            )

        records: list[ResearchSourceRecord] = list(search_failed_records)
        for index, candidate in enumerate(deduped.selected):
            if index >= fetch_budget:
                records.append(_candidate_record(candidate, error_code="fetch_budget_exhausted"))
                continue
            records.append(await self._fetch_one(candidate))
        records.extend(_duplicate_record(item) for item in deduped.duplicates)
        for record in records:
            self._write_source_record_audit(record)
        return tuple(records)

    async def _search_all(
        self, queries: Sequence[ResearchSourceQuery]
    ) -> tuple[list[ResearchSourceCandidate], list[ResearchSourceRecord]]:
        """执行全部 search，输入为搜索线列表，输出为规范化候选和搜索失败记录。"""
        candidates: list[ResearchSourceCandidate] = []
        failed_records: list[ResearchSourceRecord] = []
        for query in queries:
            try:
                raw_items = await self._provider.search(query)
            except Exception as exc:
                failed_records.append(_search_failed_record(query, self._provider.name, exc))
                self._write_audit(
                    "deep_research.search_failed",
                    {
                        "query_id": query.query_id,
                        "line": query.line,
                        "provider_name": self._provider.name,
                        "error_digest": _error_digest(exc),
                    },
                )
                continue
            limited_items = list(raw_items)[: max(query.max_results, 0)]
            candidates.extend(normalize_candidate(item) for item in limited_items)
        return candidates, failed_records

    async def _fetch_one(self, candidate: ResearchSourceCandidate) -> ResearchSourceRecord:
        """读取单个候选，输入为候选，输出为成功或失败来源记录。"""
        try:
            record = await self._provider.fetch(candidate)
        except Exception as exc:
            record = _failed_record(candidate, exc)
        record = _normalize_record(record, candidate, max_content_chars=self._max_content_chars)
        if record.status == "failed":
            self._write_audit(
                "deep_research.fetch_failed",
                {
                    "source_id": record.source_id,
                    "query_id": record.query_id,
                    "url": record.url,
                    "canonical_url": record.canonical_url,
                    "error_code": record.error_code,
                    "error_digest": record.error_message,
                    "provider_name": record.provider_name,
                },
            )
        return record

    def _write_duplicate_audit(self, duplicate: DuplicateSource) -> None:
        """写入重复来源审计，输入为重复候选和保留候选，输出为 audit 事件。"""
        self._write_audit(
            "deep_research.source_duplicate",
            {
                "source_id": duplicate.candidate.source_id,
                "query_id": duplicate.candidate.query_id,
                "canonical_url": duplicate.candidate.canonical_url,
                "kept_source_id": duplicate.kept.source_id,
                "provider_name": duplicate.candidate.provider_name,
            },
        )

    def _write_source_record_audit(self, record: ResearchSourceRecord) -> None:
        """写入完整来源记录审计，输入为来源记录，输出为包含状态和等级的 audit 事件。"""
        self._write_audit(
            "deep_research.source_recorded",
            {
                "source_id": record.source_id,
                "query_id": record.query_id,
                "provider_name": record.provider_name,
                "url": record.url,
                "canonical_url": record.canonical_url,
                "status": record.status,
                "tier": record.tier,
                "error_code": record.error_code,
                "duplicate_of": record.duplicate_of,
            },
        )

    def _write_audit(self, action: str, payload: Mapping[str, object]) -> None:
        """写入可选审计事件，输入为 action 和 payload，输出为 audit writer 追加记录。"""
        if self._audit_writer is None:
            return
        self._audit_writer.write_event({"action": action, "payload": dict(payload)})


class FakeResearchSourceProvider:
    """离线 fake provider，按内存 fixture 返回确定性 search/fetch 结果。"""

    def __init__(
        self,
        *,
        search_index: Mapping[str, Sequence[Mapping[str, object] | ResearchSourceCandidate]],
        fetch_index: Mapping[str, str | Mapping[str, object] | Exception],
        name: str = "fake_research_source",
    ) -> None:
        """初始化 fake provider，输入为 search/fetch fixture，输出为可复跑 provider。"""
        self.name = name
        self._search_index = search_index
        self._fetch_index = fetch_index

    async def search(self, query: ResearchSourceQuery) -> tuple[ResearchSourceCandidate, ...]:
        """返回搜索候选，输入为 query，输出为 fixture 中配置的候选列表。"""
        raw_items = self._search_index.get(query.query_id, self._search_index.get(query.line, ()))
        candidates = [
            _candidate_from_fixture(
                item,
                query=query,
                provider_name=self.name,
                rank=index + 1,
            )
            for index, item in enumerate(raw_items)
        ]
        return tuple(candidates[: max(query.max_results, 0)])

    async def fetch(self, candidate: ResearchSourceCandidate) -> ResearchSourceRecord:
        """读取候选内容，输入为候选，输出为 fixture 指定的来源记录或异常。"""
        key = _fetch_key(candidate, self._fetch_index)
        if key is None:
            raise LookupError(f"fake source content missing for {candidate.canonical_url}")
        value = self._fetch_index[key]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, str):
            return ResearchSourceRecord(
                source_id=candidate.source_id,
                query_id=candidate.query_id,
                url=candidate.url,
                canonical_url=candidate.canonical_url,
                title=candidate.title,
                status="fetched",
                tier="strong",
                content_text=value,
                error_code=None,
                error_message=None,
                provider_name=candidate.provider_name,
                rank=candidate.rank,
            )
        return _record_from_fixture(value, candidate)


def _candidate_from_fixture(
    item: Mapping[str, object] | ResearchSourceCandidate,
    *,
    query: ResearchSourceQuery,
    provider_name: str,
    rank: int,
) -> ResearchSourceCandidate:
    """从 fixture 构造候选，输入为 dict 或候选实例，输出为 ResearchSourceCandidate。"""
    if isinstance(item, ResearchSourceCandidate):
        return item
    url = _string(item.get("url"))
    canonical = _string(item.get("canonical_url"))
    source_id = _string(item.get("source_id"))
    if not source_id and canonical:
        source_id = stable_source_id(canonical)
    return ResearchSourceCandidate(
        source_id=source_id,
        query_id=_string(item.get("query_id")) or query.query_id,
        url=url,
        canonical_url=canonical,
        title=_string(item.get("title")),
        snippet=_string(item.get("snippet")),
        rank=_int(item.get("rank"), default=rank),
        provider_name=_string(item.get("provider_name")) or provider_name,
    )


def _record_from_fixture(
    item: Mapping[str, object],
    candidate: ResearchSourceCandidate,
) -> ResearchSourceRecord:
    """从 fixture 构造读取记录，输入为 dict 和候选，输出为 ResearchSourceRecord。"""
    return ResearchSourceRecord(
        source_id=_string(item.get("source_id")) or candidate.source_id,
        query_id=_string(item.get("query_id")) or candidate.query_id,
        url=_string(item.get("url")) or candidate.url,
        canonical_url=_string(item.get("canonical_url")) or candidate.canonical_url,
        title=_string(item.get("title")) or candidate.title,
        status=_status(item.get("status")),
        tier=_tier(item.get("tier")),
        content_text=_optional_string(item.get("content_text")),
        error_code=_optional_string(item.get("error_code")),
        error_message=_optional_string(item.get("error_message")),
        provider_name=_string(item.get("provider_name")) or candidate.provider_name,
        rank=_int(item.get("rank"), default=candidate.rank),
        duplicate_of=_optional_string(item.get("duplicate_of")),
    )


def _normalize_record(
    record: ResearchSourceRecord,
    candidate: ResearchSourceCandidate,
    *,
    max_content_chars: int,
) -> ResearchSourceRecord:
    """规范化读取记录，输入为 provider 记录和候选，输出为字段补齐和正文截断后的记录。"""
    content = record.content_text
    error_message = record.error_message
    if content is not None and len(content) > max_content_chars:
        content = content[:max_content_chars]
        error_message = "content truncated by max_content_chars"
    return replace(
        record,
        source_id=record.source_id or candidate.source_id,
        query_id=record.query_id or candidate.query_id,
        url=record.url or candidate.url,
        canonical_url=record.canonical_url or candidate.canonical_url,
        title=record.title or candidate.title,
        content_text=content,
        error_message=error_message,
        provider_name=record.provider_name or candidate.provider_name,
        rank=record.rank or candidate.rank,
    )


def _candidate_record(
    candidate: ResearchSourceCandidate, *, error_code: str
) -> ResearchSourceRecord:
    """构造未 fetch 候选记录，输入为候选和原因，输出为 weak candidate record。"""
    return ResearchSourceRecord(
        source_id=candidate.source_id,
        query_id=candidate.query_id,
        url=candidate.url,
        canonical_url=candidate.canonical_url,
        title=candidate.title,
        status="candidate",
        tier="weak",
        content_text=None,
        error_code=error_code,
        error_message="selected source was not fetched because fetch budget was exhausted",
        provider_name=candidate.provider_name,
        rank=candidate.rank,
    )


def _failed_record(candidate: ResearchSourceCandidate, exc: Exception) -> ResearchSourceRecord:
    """构造失败来源记录，输入为候选和异常，输出为 weak failed record。"""
    return ResearchSourceRecord(
        source_id=candidate.source_id,
        query_id=candidate.query_id,
        url=candidate.url,
        canonical_url=candidate.canonical_url,
        title=candidate.title,
        status="failed",
        tier="weak",
        content_text=None,
        error_code=_exception_error_code(exc),
        error_message=_exception_error_message(exc),
        provider_name=candidate.provider_name,
        rank=candidate.rank,
    )


def _search_failed_record(
    query: ResearchSourceQuery,
    provider_name: str,
    exc: Exception,
) -> ResearchSourceRecord:
    """构造搜索失败记录，输入为搜索线和异常，输出为 query-level failed source record。"""
    source_key = f"{provider_name}:{query.query_id}:search_failed"
    return ResearchSourceRecord(
        source_id=stable_source_id(source_key),
        query_id=query.query_id,
        url="",
        canonical_url="",
        title=query.line,
        status="failed",
        tier="weak",
        content_text=None,
        error_code=_exception_error_code(exc),
        error_message=_exception_error_message(exc),
        provider_name=provider_name,
        rank=0,
    )


def _duplicate_record(duplicate: DuplicateSource) -> ResearchSourceRecord:
    """构造重复来源记录，输入为重复候选，输出为 duplicate record。"""
    candidate = duplicate.candidate
    return ResearchSourceRecord(
        source_id=_duplicate_source_id(candidate, duplicate.kept.source_id),
        query_id=candidate.query_id,
        url=candidate.url,
        canonical_url=candidate.canonical_url,
        title=candidate.title,
        status="duplicate",
        tier="duplicate",
        content_text=None,
        error_code=None,
        error_message=None,
        provider_name=candidate.provider_name,
        rank=candidate.rank,
        duplicate_of=duplicate.kept.source_id,
    )


def _exception_error_code(exc: Exception) -> str:
    """提取异常错误码，输入为异常，输出可审计 error_code。"""
    value = _safe_exception_attr(exc, "error_code")
    if isinstance(value, str) and value:
        return value
    return exc.__class__.__name__.lower()


def _exception_error_message(exc: Exception) -> str:
    """提取异常摘要，输入为异常，输出可审计错误文本。"""
    value = _safe_exception_attr(exc, "error_message")
    if isinstance(value, str) and value:
        return value
    return _error_digest(exc)


def _safe_exception_attr(exc: Exception, name: str) -> object | None:
    """安全读取异常属性，输入异常和属性名，输出属性值或 None。"""
    try:
        return getattr(exc, name, None)
    except Exception:
        return None


def _duplicate_source_id(candidate: ResearchSourceCandidate, kept_source_id: str) -> str:
    """生成重复来源记录 ID，输入为重复候选和保留 ID，输出为可区分的稳定 ID。"""
    if candidate.source_id != kept_source_id:
        return candidate.source_id
    return f"{candidate.source_id}-dup-{candidate.query_id}-{candidate.rank}"


def _fetch_key(
    candidate: ResearchSourceCandidate,
    fetch_index: Mapping[str, str | Mapping[str, object] | Exception],
) -> str | None:
    """选择 fetch fixture key，输入为候选和索引，输出为匹配 key。"""
    for key in (candidate.canonical_url, candidate.url, candidate.source_id):
        if key in fetch_index:
            return key
    return None


def _error_digest(exc: Exception) -> str:
    """格式化异常摘要，输入为异常，输出为短文本。"""
    message = str(exc).strip()
    return message or exc.__class__.__name__


def _string(value: object) -> str:
    """读取字符串字段，输入为任意值，输出为去空白字符串。"""
    return value.strip() if isinstance(value, str) else ""


def _optional_string(value: object) -> str | None:
    """读取可空字符串字段，输入为任意值，输出为字符串或 None。"""
    text = _string(value)
    return text or None


def _int(value: object, *, default: int) -> int:
    """读取整数字段，输入为任意值和默认值，输出为整数。"""
    return value if isinstance(value, int) else default


def _status(value: object) -> SourceStatus:
    """读取 status 字段，输入为任意值，输出为合法来源状态。"""
    if value == "candidate":
        return "candidate"
    if value == "failed":
        return "failed"
    if value == "duplicate":
        return "duplicate"
    return "fetched"


def _tier(value: object) -> SourceTier:
    """读取 tier 字段，输入为任意值，输出为合法来源等级。"""
    if value == "weak":
        return "weak"
    if value == "duplicate":
        return "duplicate"
    return "strong"


__all__ = [
    "FakeResearchSourceProvider",
    "ResearchSourceManager",
]
