"""司天 LLM 分析层测试。

mock LLM provider，验证 analyzer 的 prompt 构造 / JSON 解析 / fallback / hash skip / disabled 路径。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from sitian.analyzer import compute_observations_hash, report_to_markdown, sitian_analyze
from sitian.config import SiTianAnalyzerConfig, SiTianInterestsConfig
from sitian.models import SiTianObservation, SiTianReport


def _obs(
    *,
    entity_type: str = "project",
    entity_key: str = "/test/proj",
    payload: dict[str, Any] | None = None,
) -> SiTianObservation:
    return SiTianObservation(
        id="obs-1",
        source_id="src-1",
        source_kind="claude_workspace",
        observed_at="2026-05-16T10:00:00Z",
        entity_type=entity_type,
        entity_key=entity_key,
        payload=payload
        or {
            "displayName": "test-project",
            "sessionCount": 2,
            "lastModifiedAt": "2026-05-16T09:00:00Z",
            "recentSessions": [
                {
                    "threadId": "t1",
                    "lastModified": "2026-05-16T09:00:00Z",
                    "recentUserMessages": ["帮我看看这个 bug"],
                    "recentAssistantMessages": ["我先检查一下代码"],
                }
            ],
        },
        evidence_refs=(),
    )


_VALID_LLM_RESPONSE = json.dumps(
    {
        "summary": "1 个项目活跃，正在修 bug",
        "items": [
            {
                "projectId": "/test/proj",
                "projectName": "test-project",
                "status": "active",
                "severity": "medium",
                "narrative": "正在排查一个 bug，最近有 2 个 session 活跃",
                "recentActivity": "最近 1 小时有 2 个 session",
                "nextActions": ["修复 bug", "跑测试"],
            }
        ],
    },
    ensure_ascii=False,
)


@dataclass
class _MockLLMResponse:
    message: Any
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    raw: Any = None


@dataclass
class _MockMessage:
    role: str = "assistant"
    content: str | None = None
    tool_calls: Any = None
    tool_call_id: Any = None
    name: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


class _MockProvider:
    def __init__(
        self, content: str = _VALID_LLM_RESPONSE, *, raise_on_call: Exception | None = None
    ) -> None:
        self._content = content
        self._raise = raise_on_call
        self.call_count = 0
        self.last_request: Any = None

    async def complete(self, request: Any) -> _MockLLMResponse:
        self.call_count += 1
        self.last_request = request
        if self._raise is not None:
            raise self._raise
        return _MockLLMResponse(message=_MockMessage(content=self._content))


_DEFAULT_ANALYZER = SiTianAnalyzerConfig(
    enabled=True,
    model_name="test-model",
    base_url="http://127.0.0.1:1234/v1",
    max_tokens=1024,
    temperature=0.3,
    timeout=10,
    max_context_chars=50000,
    skip_if_unchanged=False,
)
_DEFAULT_INTERESTS = SiTianInterestsConfig()


@pytest.mark.unit
def test_analyze_normal_llm_response() -> None:
    provider = _MockProvider()
    report = asyncio.run(
        sitian_analyze(
            (_obs(),),
            provider=provider,
            analyzer_config=_DEFAULT_ANALYZER,
            interests_config=_DEFAULT_INTERESTS,
            observed_at="2026-05-16T10:00:00Z",
        )
    )
    assert isinstance(report, SiTianReport)
    assert report.summary == "1 个项目活跃，正在修 bug"
    assert len(report.items) == 1
    assert report.items[0].project_name == "test-project"
    assert report.items[0].status == "active"
    assert report.items[0].next_actions == ("修复 bug", "跑测试")
    assert report.errors == ()
    assert report.model_name == "test-model"
    assert provider.call_count == 1


@pytest.mark.unit
def test_analyze_llm_call_failure_returns_error_report() -> None:
    provider = _MockProvider(raise_on_call=TimeoutError("LLM timeout"))
    report = asyncio.run(
        sitian_analyze(
            (_obs(),),
            provider=provider,
            analyzer_config=_DEFAULT_ANALYZER,
            interests_config=_DEFAULT_INTERESTS,
            observed_at="2026-05-16T10:00:00Z",
        )
    )
    assert report.errors != ()
    assert "LLM call failed" in report.errors[0]
    assert report.items == ()


@pytest.mark.unit
def test_analyze_json_parse_failure_returns_error_report() -> None:
    provider = _MockProvider(content="this is not json {{{")
    report = asyncio.run(
        sitian_analyze(
            (_obs(),),
            provider=provider,
            analyzer_config=_DEFAULT_ANALYZER,
            interests_config=_DEFAULT_INTERESTS,
            observed_at="2026-05-16T10:00:00Z",
        )
    )
    assert report.errors != ()
    assert "JSON parse failed" in report.errors[0]


@pytest.mark.unit
def test_analyze_no_project_observations_returns_empty() -> None:
    provider = _MockProvider()
    thread_obs = _obs(entity_type="thread", entity_key="t1")
    report = asyncio.run(
        sitian_analyze(
            (thread_obs,),
            provider=provider,
            analyzer_config=_DEFAULT_ANALYZER,
            interests_config=_DEFAULT_INTERESTS,
            observed_at="2026-05-16T10:00:00Z",
        )
    )
    assert report.summary == "无可分析的项目数据"
    assert report.items == ()
    assert provider.call_count == 0


@pytest.mark.unit
def test_analyze_interests_filter_excludes_project() -> None:
    provider = _MockProvider()
    interests = SiTianInterestsConfig(projects=["/other/proj"])
    report = asyncio.run(
        sitian_analyze(
            (_obs(),),
            provider=provider,
            analyzer_config=_DEFAULT_ANALYZER,
            interests_config=interests,
            observed_at="2026-05-16T10:00:00Z",
        )
    )
    assert report.summary == "无可分析的项目数据"
    assert provider.call_count == 0


@pytest.mark.unit
def test_analyze_context_truncation() -> None:
    large_obs = _obs(
        payload={
            "displayName": "big-project",
            "sessionCount": 1,
            "lastModifiedAt": "2026-05-16T09:00:00Z",
            "recentSessions": [
                {
                    "threadId": "t1",
                    "lastModified": "2026-05-16T09:00:00Z",
                    "recentUserMessages": ["x" * 10000],
                    "recentAssistantMessages": ["y" * 10000],
                }
            ],
        }
    )
    small_obs = _obs(
        entity_key="/test/small",
        payload={
            "displayName": "small-project",
            "sessionCount": 1,
            "lastModifiedAt": "2026-05-16T08:00:00Z",
            "recentSessions": [],
        },
    )
    provider = _MockProvider()
    analyzer = SiTianAnalyzerConfig(
        enabled=True,
        model_name="test-model",
        base_url="http://127.0.0.1:1234/v1",
        max_context_chars=100,
        skip_if_unchanged=False,
    )
    asyncio.run(
        sitian_analyze(
            (large_obs, small_obs),
            provider=provider,
            analyzer_config=analyzer,
            interests_config=_DEFAULT_INTERESTS,
            observed_at="2026-05-16T10:00:00Z",
        )
    )
    assert provider.call_count == 1
    request = provider.last_request
    user_content = request.messages[1].content
    parsed = json.loads(user_content)
    assert len(parsed) == 1
    assert parsed[0]["projectName"] == "big-project"


@pytest.mark.unit
def test_observations_hash_deterministic() -> None:
    obs = (_obs(),)
    h1 = compute_observations_hash(obs)
    h2 = compute_observations_hash(obs)
    assert h1 == h2
    assert len(h1) == 64


@pytest.mark.unit
def test_report_to_markdown_contains_key_sections() -> None:
    report = SiTianReport(
        report_id="test_001",
        generated_at="2026-05-16T10:00:00Z",
        summary="测试总结",
        items=(),
        model_name="test-model",
        errors=("test error",),
    )
    md = report_to_markdown(report)
    assert "# 司天分析报告" in md
    assert "测试总结" in md
    assert "test error" in md
    assert "test-model" in md
