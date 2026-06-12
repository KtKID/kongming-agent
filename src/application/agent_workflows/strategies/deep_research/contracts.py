"""Deep Research 来源数据合同。

本脚本定义来源检索、来源候选、来源读取结果和 provider protocol。
作用是让 DeepResearchStrategy、source manager、fake provider 和测试使用同一组结构化字段。
关键执行流程：planner 产出 ResearchSourceQuery，provider 返回 ResearchSourceCandidate，manager 归一化为 ResearchSourceRecord。
关键函数：ResearchSourceProvider.search 检索候选来源，ResearchSourceProvider.fetch 读取候选来源。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

SourceStatus = Literal["candidate", "fetched", "failed", "duplicate"]
SourceTier = Literal["strong", "weak", "duplicate"]


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


__all__ = [
    "ResearchSourceCandidate",
    "ResearchSourceProvider",
    "ResearchSourceQuery",
    "ResearchSourceRecord",
    "SourceStatus",
    "SourceTier",
]
