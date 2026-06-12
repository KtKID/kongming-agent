"""Deep Research URL 去重与预算裁剪。

本脚本负责把 search 返回的 URL 规范化、生成稳定 source_id、折叠重复来源并记录预算溢出。
作用是让后续 fetch 和 Extract 阶段只处理唯一且可追溯的来源集合。
关键执行流程：canonicalize_url 归一化 URL，SourceDeduper.select 按顺序保留唯一 URL，超出预算进入 overflow。
关键函数：canonicalize_url 规范化 URL，stable_source_id 生成来源 ID，SourceDeduper.select 执行去重选择。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from application.agent_workflows.strategies.deep_research.contracts import (
    ResearchSourceCandidate,
)

_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
        "yclid",
    }
)


@dataclass(frozen=True)
class DuplicateSource:
    """重复来源记录，输入为丢弃候选和保留候选，输出给 audit 和 duplicate record。"""

    candidate: ResearchSourceCandidate
    kept: ResearchSourceCandidate


@dataclass(frozen=True)
class SourceDedupeResult:
    """来源去重结果，输入为候选列表和预算，输出为保留、重复和溢出三类候选。"""

    selected: tuple[ResearchSourceCandidate, ...]
    duplicates: tuple[DuplicateSource, ...]
    overflow: tuple[ResearchSourceCandidate, ...]


class SourceDeduper:
    """按 canonical URL 选择唯一来源。"""

    def select(
        self,
        candidates: list[ResearchSourceCandidate],
        *,
        source_budget: int,
    ) -> SourceDedupeResult:
        """执行去重选择，输入为候选和预算，输出为 selected/duplicates/overflow。"""
        if source_budget < 0:
            raise ValueError("source_budget must be >= 0")

        selected: list[ResearchSourceCandidate] = []
        duplicates: list[DuplicateSource] = []
        overflow: list[ResearchSourceCandidate] = []
        kept_by_url: dict[str, ResearchSourceCandidate] = {}

        for candidate in candidates:
            normalized = normalize_candidate(candidate)
            kept = kept_by_url.get(normalized.canonical_url)
            if kept is not None:
                duplicates.append(DuplicateSource(candidate=normalized, kept=kept))
                continue
            if len(selected) >= source_budget:
                overflow.append(normalized)
                continue
            kept_by_url[normalized.canonical_url] = normalized
            selected.append(normalized)

        return SourceDedupeResult(
            selected=tuple(selected),
            duplicates=tuple(duplicates),
            overflow=tuple(overflow),
        )


def normalize_candidate(candidate: ResearchSourceCandidate) -> ResearchSourceCandidate:
    """补齐候选规范字段，输入为 provider 候选，输出为 canonical URL 和 source_id 稳定的候选。"""
    canonical = candidate.canonical_url.strip() or canonicalize_url(candidate.url)
    source_id = candidate.source_id.strip() or stable_source_id(canonical)
    return ResearchSourceCandidate(
        source_id=source_id,
        query_id=candidate.query_id,
        url=candidate.url,
        canonical_url=canonical,
        title=candidate.title,
        snippet=candidate.snippet,
        rank=candidate.rank,
        provider_name=candidate.provider_name,
    )


def canonicalize_url(url: str) -> str:
    """规范化 URL，输入为原始 URL，输出为去跟踪参数、排序 query 和去尾 slash 的 URL。"""
    raw = url.strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host
    if port is not None and not _is_default_port(scheme, port):
        netloc = f"{host}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query_items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    query = urlencode(sorted(query_items), doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def stable_source_id(canonical_url: str) -> str:
    """生成稳定来源 ID，输入为 canonical URL，输出为 src- 前缀短 hash。"""
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:12]
    return f"src-{digest}"


def _is_default_port(scheme: str, port: int) -> bool:
    """判断端口是否为协议默认端口，输入为 scheme 和端口，输出为布尔值。"""
    return (scheme == "http" and port == 80) or (scheme == "https" and port == 443)


__all__ = [
    "DuplicateSource",
    "SourceDedupeResult",
    "SourceDeduper",
    "canonicalize_url",
    "normalize_candidate",
    "stable_source_id",
]
