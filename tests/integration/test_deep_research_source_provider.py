"""Deep Research source provider 集成测试。

本脚本验证 fake provider 到 ResearchSourceManager 的 provider-only 完整收集链路。
作用是保证来源记录可直接作为后续 Extract phase 输入，并且审计事件随链路写入。
关键执行流程：构造多 query fixture，调用 collect_sources，断言 fetched/failed/duplicate/candidate records 和 audit。
关键函数：test_provider_only_collection_outputs_extract_ready_records 执行完整 provider-only 链路。
"""

from __future__ import annotations

import pytest

from application.agent_workflows.strategies.deep_research import (
    FakeResearchSourceProvider,
    ResearchSourceManager,
    ResearchSourceQuery,
)


class _AuditRecorder:
    """测试用 audit writer，记录 provider-only 链路事件。"""

    def __init__(self) -> None:
        """初始化 recorder，输入为空，输出为可收集事件的实例。"""
        self.events: list[dict[str, object]] = []

    def write_event(self, event: dict[str, object]) -> None:
        """记录审计事件，输入为事件 dict，输出为追加到内存。"""
        self.events.append(event)


@pytest.mark.asyncio
async def test_provider_only_collection_outputs_extract_ready_records() -> None:
    """验证 provider-only 集成链路，输入为 fake fixture，输出为可提取事实的来源记录。"""
    provider = FakeResearchSourceProvider(
        search_index={
            "overview": [
                {"url": "https://example.com/guide?utm_medium=social", "title": "Guide"},
                {"url": "https://example.com/changelog", "title": "Changelog"},
            ],
            "skeptical": [
                {"url": "https://www.example.com/guide/", "title": "Guide mirror"},
                {"url": "https://example.com/fail", "title": "Failing source"},
            ],
        },
        fetch_index={
            "https://example.com/guide": "official guide body",
            "https://example.com/changelog": "release changelog body",
            "https://example.com/fail": OSError("fetch refused"),
        },
    )
    audit = _AuditRecorder()
    manager = ResearchSourceManager(provider, audit_writer=audit)

    records = await manager.collect_sources(
        [
            ResearchSourceQuery(
                query_id="overview",
                line="deep research overview",
                intent="overview",
                max_results=4,
            ),
            ResearchSourceQuery(
                query_id="skeptical",
                line="deep research skeptical",
                intent="skeptical",
                max_results=4,
            ),
        ],
        source_budget=4,
        fetch_budget=3,
    )

    statuses = [record.status for record in records]
    actions = {str(event["action"]) for event in audit.events}

    assert statuses == ["fetched", "fetched", "failed", "duplicate"]
    assert all(record.source_id for record in records)
    assert records[0].content_text == "official guide body"
    assert records[2].tier == "weak"
    assert records[3].duplicate_of == records[0].source_id
    assert {
        "deep_research.source_selected",
        "deep_research.source_duplicate",
        "deep_research.fetch_failed",
        "deep_research.source_recorded",
    }.issubset(actions)
