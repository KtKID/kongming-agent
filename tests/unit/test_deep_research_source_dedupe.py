"""Deep Research URL 去重单元测试。

本脚本验证 canonical URL、稳定 source_id、重复来源映射和 source budget overflow。
作用是固定 Search 阶段的 URL 归一化和预算裁剪语义，避免后续 Extract 阶段看到不稳定来源集合。
关键执行流程：构造候选 URL，调用 SourceDeduper.select，断言 selected/duplicates/overflow 三类输出。
关键函数：_candidate 构造测试候选，test_* 覆盖 canonicalize、duplicate 和 overflow。
"""

from __future__ import annotations

from application.agent_workflows.strategies.deep_research import (
    ResearchSourceCandidate,
    SourceDeduper,
    canonicalize_url,
    stable_source_id,
)


def _candidate(url: str, *, query_id: str = "q1", rank: int = 1) -> ResearchSourceCandidate:
    """构造候选来源，输入为 URL/query/rank，输出为空 source_id 的候选对象。"""
    return ResearchSourceCandidate(
        source_id="",
        query_id=query_id,
        url=url,
        canonical_url="",
        title=f"title {rank}",
        snippet=f"snippet {rank}",
        rank=rank,
        provider_name="fake",
    )


def test_canonicalize_url_strips_tracking_params_and_sorts_query() -> None:
    """验证 URL 归一化，输入为带跟踪参数的 URL，输出为稳定 canonical URL。"""
    result = canonicalize_url("HTTPS://www.Example.com:443/path/?utm_source=news&b=2&a=1#section")

    assert result == "https://example.com/path?a=1&b=2"


def test_stable_source_id_uses_canonical_url_digest() -> None:
    """验证 source_id 稳定性，输入为同一 canonical URL，输出为相同短 hash。"""
    canonical = "https://example.com/path"

    assert stable_source_id(canonical) == stable_source_id(canonical)
    assert stable_source_id(canonical).startswith("src-")


def test_deduper_records_duplicate_and_overflow() -> None:
    """验证去重和溢出，输入为重复和超预算候选，输出为三类结果。"""
    candidates = [
        _candidate("https://example.com/a?utm_source=x", query_id="q1", rank=1),
        _candidate("https://www.example.com/a/", query_id="q2", rank=1),
        _candidate("https://example.com/b", query_id="q1", rank=2),
        _candidate("https://example.com/c", query_id="q1", rank=3),
    ]

    result = SourceDeduper().select(candidates, source_budget=2)

    assert [item.canonical_url for item in result.selected] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert len(result.duplicates) == 1
    assert result.duplicates[0].kept.source_id == result.selected[0].source_id
    assert result.duplicates[0].candidate.query_id == "q2"
    assert [item.canonical_url for item in result.overflow] == ["https://example.com/c"]
