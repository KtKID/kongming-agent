"""Deep Research source provider 单元测试。

本脚本验证 ResearchSourceManager、FakeResearchSourceProvider、失败降级、预算裁剪和审计 payload。
作用是固定 Deep Research 来源输入的第一层可运行语义，确保后续 Extract 阶段可以消费稳定 record。
关键执行流程：用 fake provider 构造 search/fetch fixture，调用 collect_sources，断言来源记录和 audit 事件。
关键函数：_query 构造搜索线，_AuditRecorder 收集审计事件，test_* 覆盖主链路和失败路径。
"""

from __future__ import annotations

import pytest

from application.agent_workflows.strategies.deep_research import (
    FakeResearchSourceProvider,
    ResearchSourceCandidate,
    ResearchSourceManager,
    ResearchSourceQuery,
)


class _AuditRecorder:
    """测试用 audit writer，记录 write_event 输入。"""

    def __init__(self) -> None:
        """初始化 recorder，输入为空，输出为可收集事件的实例。"""
        self.events: list[dict[str, object]] = []

    def write_event(self, event: dict[str, object]) -> None:
        """记录审计事件，输入为事件 dict，输出为追加到内存列表。"""
        self.events.append(event)


class _ExplodingAttributeError(RuntimeError):
    """测试用异常，读取错误属性时抛错。"""

    @property
    def error_code(self) -> str:
        """模拟 provider 暴露坏属性，输入为空，输出异常。"""
        raise RuntimeError("error_code property exploded")

    @property
    def error_message(self) -> str:
        """模拟 provider 暴露坏属性，输入为空，输出异常。"""
        raise RuntimeError("error_message property exploded")


class _ExplodingSearchProvider:
    """测试用 provider，search 阶段抛出坏属性异常。"""

    name = "exploding_search_provider"

    async def search(self, query: ResearchSourceQuery) -> tuple[ResearchSourceCandidate, ...]:
        """搜索时抛错，输入 query，输出异常。"""
        del query
        raise _ExplodingAttributeError("search unstable")

    async def fetch(self, candidate: ResearchSourceCandidate) -> object:
        """不会被调用，输入候选，输出异常。"""
        raise AssertionError(f"fetch should not run: {candidate.url}")


def _query(query_id: str = "q1", *, max_results: int = 10) -> ResearchSourceQuery:
    """构造搜索线，输入为 query_id 和 max_results，输出为 ResearchSourceQuery。"""
    return ResearchSourceQuery(
        query_id=query_id,
        line=f"query {query_id}",
        intent="overview",
        max_results=max_results,
    )


@pytest.mark.asyncio
async def test_collect_sources_fetches_unique_records_and_audits_duplicates() -> None:
    """验证主链路，输入为重复 URL fixture，输出为 fetched 和 duplicate records。"""
    provider = FakeResearchSourceProvider(
        search_index={
            "q1": [
                {"url": "https://example.com/a?utm_source=x", "title": "A"},
                {"url": "https://example.com/b", "title": "B"},
            ],
            "q2": [
                {"url": "https://www.example.com/a/", "title": "A duplicate"},
            ],
        },
        fetch_index={
            "https://example.com/a": "alpha content",
            "https://example.com/b": "beta content",
        },
    )
    audit = _AuditRecorder()
    manager = ResearchSourceManager(provider, audit_writer=audit)

    records = await manager.collect_sources(
        [_query("q1"), _query("q2")],
        source_budget=5,
        fetch_budget=5,
    )

    assert [record.status for record in records] == ["fetched", "fetched", "duplicate"]
    assert records[0].content_text == "alpha content"
    assert records[2].duplicate_of == records[0].source_id
    assert records[2].source_id != records[2].duplicate_of
    assert "deep_research.source_duplicate" in _actions(audit)
    recorded_payloads = _payloads(audit, "deep_research.source_recorded")
    assert recorded_payloads[-1]["status"] == "duplicate"
    assert recorded_payloads[-1]["tier"] == "duplicate"
    assert recorded_payloads[-1]["duplicate_of"] == records[0].source_id


@pytest.mark.asyncio
async def test_collect_sources_records_fetch_failure_as_weak_source() -> None:
    """验证 fetch 失败降级，输入为异常 fixture，输出为 failed weak source。"""
    provider = FakeResearchSourceProvider(
        search_index={"q1": [{"url": "https://example.com/fail", "title": "Fail"}]},
        fetch_index={"https://example.com/fail": RuntimeError("network down")},
    )
    audit = _AuditRecorder()
    manager = ResearchSourceManager(provider, audit_writer=audit)

    records = await manager.collect_sources([_query()], source_budget=5, fetch_budget=5)

    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].tier == "weak"
    assert records[0].error_code == "runtimeerror"
    assert "network down" in str(records[0].error_message)
    assert "deep_research.fetch_failed" in _actions(audit)


@pytest.mark.asyncio
async def test_collect_sources_handles_exception_error_properties_that_raise() -> None:
    """验证异常属性二次抛错，输入 fetch 坏异常，输出稳定 failed record。"""
    provider = FakeResearchSourceProvider(
        search_index={"q1": [{"url": "https://example.com/fail", "title": "Fail"}]},
        fetch_index={"https://example.com/fail": _ExplodingAttributeError("fetch unstable")},
    )
    manager = ResearchSourceManager(provider)

    records = await manager.collect_sources([_query()], source_budget=5, fetch_budget=5)

    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].error_code == "_explodingattributeerror"
    assert records[0].error_message == "fetch unstable"


@pytest.mark.asyncio
async def test_collect_sources_handles_search_exception_error_properties_that_raise() -> None:
    """验证 search 异常属性二次抛错，输入 search 坏异常，输出稳定 failed record。"""
    manager = ResearchSourceManager(_ExplodingSearchProvider())

    records = await manager.collect_sources([_query()], source_budget=5, fetch_budget=5)

    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].error_code == "_explodingattributeerror"
    assert records[0].error_message == "search unstable"


@pytest.mark.asyncio
async def test_collect_sources_respects_source_and_fetch_budget() -> None:
    """验证预算裁剪，输入为三个候选和 fetch_budget=1，输出为 fetched/candidate 和 overflow audit。"""
    provider = FakeResearchSourceProvider(
        search_index={
            "q1": [
                {"url": "https://example.com/a", "title": "A"},
                {"url": "https://example.com/b", "title": "B"},
                {"url": "https://example.com/c", "title": "C"},
            ]
        },
        fetch_index={
            "https://example.com/a": "alpha",
            "https://example.com/b": "beta",
            "https://example.com/c": "gamma",
        },
    )
    audit = _AuditRecorder()
    manager = ResearchSourceManager(provider, audit_writer=audit)

    records = await manager.collect_sources([_query()], source_budget=2, fetch_budget=1)

    assert [record.status for record in records] == ["fetched", "candidate"]
    assert records[1].error_code == "fetch_budget_exhausted"
    assert "deep_research.source_overflow" in _actions(audit)


@pytest.mark.asyncio
async def test_fake_provider_returns_empty_results_for_missing_query() -> None:
    """验证空结果，输入为缺失 query fixture，输出为空来源列表。"""
    provider = FakeResearchSourceProvider(search_index={}, fetch_index={})
    manager = ResearchSourceManager(provider)

    records = await manager.collect_sources([_query("missing")], source_budget=5, fetch_budget=5)

    assert records == ()


def _actions(audit: _AuditRecorder) -> set[str]:
    """提取审计 action，输入为 recorder，输出为 action 集合。"""
    return {str(event["action"]) for event in audit.events}


def _payloads(audit: _AuditRecorder, action: str) -> list[dict[str, object]]:
    """按 action 提取 payload，输入为 recorder 和 action，输出为 payload 列表。"""
    return [
        event["payload"]
        for event in audit.events
        if event["action"] == action and isinstance(event["payload"], dict)
    ]
