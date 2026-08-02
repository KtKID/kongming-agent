"""司天 LLM 分析层 V2 测试。

mock LLM provider，覆盖 alert 解析 / topAlerts 排序 / statusByRule 规则 /
sessionStats 计数 / fallback / dispatch 占位字段。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from sitian.analyzer import (
    _compute_session_stats,
    _compute_status_by_rule,
    _normalize_alert,
    _normalize_type_slug,
    _sort_top_alerts,
    compute_observations_hash,
    report_to_markdown,
    sitian_analyze,
)
from sitian.config import SiTianAnalyzerConfig, SiTianInterestsConfig
from sitian.models import SiTianAlert, SiTianObservation, SiTianReport

# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _project_obs(
    *,
    project_id: str = "/test/proj",
    project_name: str = "test-project",
    cwd: str | None = None,
    last_modified_at: str = "2026-05-16T09:00:00Z",
    session_count: int = 2,
    recent_sessions: list[dict[str, Any]] | None = None,
) -> SiTianObservation:
    return SiTianObservation(
        id=f"obs-{project_id}",
        source_id="src-1",
        source_kind="claude_workspace",
        observed_at="2026-05-16T10:00:00Z",
        entity_type="project",
        entity_key=project_id,
        payload={
            "displayName": project_name,
            "cwd": cwd or project_id,
            "sessionCount": session_count,
            "lastModifiedAt": last_modified_at,
            "recentSessions": recent_sessions
            or [
                {
                    "threadId": "t1",
                    "lastModified": last_modified_at,
                    "recentUserMessages": ["帮我看 bug"],
                    "recentAssistantMessages": ["我来分析"],
                }
            ],
        },
        evidence_refs=(),
    )


def _thread_obs(
    *,
    thread_id: str = "t1",
    cwd: str = "/test/proj",
    last_modified_at: str = "2026-05-16T09:00:00Z",
) -> SiTianObservation:
    return SiTianObservation(
        id=f"obs-thread-{thread_id}",
        source_id="src-1",
        source_kind="claude_workspace",
        observed_at="2026-05-16T10:00:00Z",
        entity_type="thread",
        entity_key=thread_id,
        payload={
            "threadId": thread_id,
            "cwd": cwd,
            "lastModifiedAt": last_modified_at,
            "recentUserMessages": ["帮我看 bug"],
            "recentAssistantMessages": ["我来分析"],
        },
        evidence_refs=(),
    )


_VALID_LLM_RESPONSE = json.dumps(
    {
        "summary": "1 个项目活跃，发现 1 个 P1 bug",
        "alerts": [
            {
                "sessionId": "t1-session-uuid-001",
                "projectId": "/test/proj",
                "projectName": "test-project",
                "typeSlug": "whiteboard server stale",
                "priority": "P1",
                "severity": "high",
                "severityReasons": ["blocking other work", "404 errors observed"],
                "title": "白板接口异常",
                "displayMessage": "我发现白板 API 一直 404",
                "evidence": [{"threadId": "t1-session-uuid-001", "quote": "404 Not Found"}],
                "recommendation": "重启 web server",
                "instructionDraft": "请重启 web server 并验证 API",
            }
        ],
        "projects": [
            {
                "projectId": "/test/proj",
                "projectName": "test-project",
                "statusReason": "lastModifiedAt within 24h",
                "narrative": "项目活跃，1 个待处理 bug",
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
    preset_id="test-model",
    reasoning_effort="high",
    max_context_chars=50000,
    skip_if_unchanged=False,
)
_DEFAULT_INTERESTS = SiTianInterestsConfig()


# ---------------------------------------------------------------------------
# 主流程：alert 解析 + 规范化
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_analyze_normal_response_produces_alert_and_project() -> None:
    provider = _MockProvider()
    report = asyncio.run(
        sitian_analyze(
            (_project_obs(),),
            provider=provider,
            analyzer_config=_DEFAULT_ANALYZER,
            interests_config=_DEFAULT_INTERESTS,
            observed_at="2026-05-16T10:00:00Z",
        )
    )
    assert isinstance(report, SiTianReport)
    assert report.summary == "1 个项目活跃，发现 1 个 P1 bug"
    assert provider.last_request.model == ""
    assert provider.last_request.reasoning_effort == "high"
    assert provider.last_request.temperature is None
    assert provider.last_request.max_tokens is None
    assert len(report.top_alerts) == 1
    assert len(report.projects) == 1

    alert = report.top_alerts[0]
    assert alert.priority == "P1"
    assert alert.severity == "high"
    assert alert.title == "白板接口异常"
    # alert_id 由脚本生成（typeSlug 已规范化）
    assert alert.alert_id == "proj:t1-sessi:whiteboard-server-stale"
    assert alert.dedupe_key == "proj:whiteboard-server-stale"
    # dispatch 占位字段存在
    assert alert.dispatch["targetThreadId"] == "t1-session-uuid-001"
    assert alert.dispatch["instructionDraft"] == "请重启 web server 并验证 API"

    project = report.projects[0]
    assert project.status_by_rule == "active"
    assert project.session_stats["total"] == 2  # 来自 sessionCount
    assert project.alert_ids == (alert.alert_id,)


# ---------------------------------------------------------------------------
# fallback 路径：LLM 失败 / JSON 解析失败
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_analyze_llm_failure_returns_error_report_with_rule_snapshots() -> None:
    """LLM 失败时仍返回脚本算的 project snapshot（不是空）。"""
    provider = _MockProvider(raise_on_call=TimeoutError("LLM timeout"))
    report = asyncio.run(
        sitian_analyze(
            (_project_obs(),),
            provider=provider,
            analyzer_config=_DEFAULT_ANALYZER,
            interests_config=_DEFAULT_INTERESTS,
            observed_at="2026-05-16T10:00:00Z",
        )
    )
    assert report.errors != ()
    assert "LLM call failed" in report.errors[0]
    assert report.top_alerts == ()
    # fallback 仍含 project snapshot（含 statusByRule）
    assert len(report.projects) == 1
    assert report.projects[0].status_by_rule == "active"
    assert report.projects[0].alert_ids == ()


@pytest.mark.unit
def test_analyze_json_parse_failure_returns_error_report() -> None:
    provider = _MockProvider(content="not json {{{")
    report = asyncio.run(
        sitian_analyze(
            (_project_obs(),),
            provider=provider,
            analyzer_config=_DEFAULT_ANALYZER,
            interests_config=_DEFAULT_INTERESTS,
            observed_at="2026-05-16T10:00:00Z",
        )
    )
    assert report.errors != ()
    assert "JSON parse failed" in report.errors[0]
    assert report.top_alerts == ()
    assert len(report.projects) == 1  # fallback snapshot 仍在


@pytest.mark.unit
def test_analyze_no_project_observations_returns_empty() -> None:
    provider = _MockProvider()
    report = asyncio.run(
        sitian_analyze(
            (),
            provider=provider,
            analyzer_config=_DEFAULT_ANALYZER,
            interests_config=_DEFAULT_INTERESTS,
            observed_at="2026-05-16T10:00:00Z",
        )
    )
    assert report.summary == "无可分析的项目数据"
    assert report.top_alerts == ()
    assert report.projects == ()
    assert provider.call_count == 0


# ---------------------------------------------------------------------------
# statusByRule 规则
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_by_rule_active_within_24h() -> None:
    payload = {"lastModifiedAt": "2026-05-16T09:00:00Z"}  # 1h 前
    assert _compute_status_by_rule(payload, "2026-05-16T10:00:00Z") == "active"


@pytest.mark.unit
def test_status_by_rule_idle_within_3d() -> None:
    payload = {"lastModifiedAt": "2026-05-14T10:00:00Z"}  # 2 天前
    assert _compute_status_by_rule(payload, "2026-05-16T10:00:00Z") == "idle"


@pytest.mark.unit
def test_status_by_rule_dormant_over_3d() -> None:
    payload = {"lastModifiedAt": "2026-05-10T10:00:00Z"}  # 6 天前
    assert _compute_status_by_rule(payload, "2026-05-16T10:00:00Z") == "dormant"


@pytest.mark.unit
def test_status_by_rule_empty_when_no_last_modified() -> None:
    assert _compute_status_by_rule({}, "2026-05-16T10:00:00Z") == "empty"


# ---------------------------------------------------------------------------
# sessionStats 计数
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_session_stats_buckets_by_recency() -> None:
    threads = [
        _thread_obs(thread_id="recent", last_modified_at="2026-05-16T09:30:00Z"),  # 30 min ago
        _thread_obs(thread_id="today", last_modified_at="2026-05-15T20:00:00Z"),  # 14h ago
        _thread_obs(thread_id="3d", last_modified_at="2026-05-14T11:00:00Z"),  # 47h ago
        _thread_obs(thread_id="old", last_modified_at="2026-05-10T10:00:00Z"),  # 6 days ago
    ]
    stats = _compute_session_stats(
        threads, project_payload={"sessionCount": 4}, observed_at="2026-05-16T10:00:00Z"
    )
    assert stats == {
        "total": 4,
        "activeWithin1h": 1,  # recent
        "activeWithin24h": 2,  # recent + today
        "activeWithin3d": 3,  # recent + today + 3d
    }


# ---------------------------------------------------------------------------
# topAlerts 排序：priority → severity → recency
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_top_alerts_sort_priority_severity_recency() -> None:
    p1_high_old = _build_alert(
        alert_id="a:p1high-old",
        priority="P1",
        severity="high",
        session_id="s-old",
    )
    p0_low = _build_alert(
        alert_id="b:p0low",
        priority="P0",
        severity="low",
        session_id="s-mid",
    )
    p1_high_new = _build_alert(
        alert_id="c:p1high-new",
        priority="P1",
        severity="high",
        session_id="s-new",
    )
    p2_high = _build_alert(
        alert_id="d:p2high",
        priority="P2",
        severity="high",
        session_id="s-mid",
    )
    project_threads = {
        "/p": [
            _thread_obs(thread_id="s-new", last_modified_at="2026-05-16T09:30:00Z"),
            _thread_obs(thread_id="s-mid", last_modified_at="2026-05-15T10:00:00Z"),
            _thread_obs(thread_id="s-old", last_modified_at="2026-05-14T10:00:00Z"),
        ]
    }
    sorted_alerts = _sort_top_alerts((p1_high_old, p0_low, p1_high_new, p2_high), project_threads)
    # P0 first, then P1 (newer first), then P2
    assert [a.alert_id for a in sorted_alerts] == [
        "b:p0low",
        "c:p1high-new",
        "a:p1high-old",
        "d:p2high",
    ]


# ---------------------------------------------------------------------------
# typeSlug 规范化 + alertId 生成
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_normalize_type_slug() -> None:
    assert _normalize_type_slug("Whiteboard Server Stale") == "whiteboard-server-stale"
    assert _normalize_type_slug("API 404!") == "api-404"
    assert _normalize_type_slug("") == "unknown"
    assert _normalize_type_slug("___") == "unknown"


@pytest.mark.unit
def test_normalize_alert_generates_dispatch_placeholder() -> None:
    """LLM 输出 instructionDraft → SiTianAlert.dispatch.instructionDraft；
    targetThreadId 默认等于 sessionId（task 1 占位，task 2 才用）。"""
    raw = {
        "sessionId": "session-abc-123",
        "projectId": "/proj/foo",
        "projectName": "foo",
        "typeSlug": "test-type",
        "priority": "P1",
        "severity": "medium",
        "title": "测试",
        "displayMessage": "测试",
        "recommendation": "测试",
        "instructionDraft": "请测试",
    }
    alert = _normalize_alert(raw)
    assert alert.dispatch["targetThreadId"] == "session-abc-123"
    assert alert.dispatch["instructionDraft"] == "请测试"
    assert alert.alert_id == "foo:session-:test-type"
    assert alert.dedupe_key == "foo:test-type"


# ---------------------------------------------------------------------------
# interests 过滤 + observations hash
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_interests_filter_excludes_project() -> None:
    provider = _MockProvider()
    interests = SiTianInterestsConfig(projects=["/other/proj"])
    report = asyncio.run(
        sitian_analyze(
            (_project_obs(),),
            provider=provider,
            analyzer_config=_DEFAULT_ANALYZER,
            interests_config=interests,
            observed_at="2026-05-16T10:00:00Z",
        )
    )
    assert report.summary == "无可分析的项目数据"
    assert provider.call_count == 0


@pytest.mark.unit
def test_observations_hash_deterministic() -> None:
    obs = (_project_obs(),)
    h1 = compute_observations_hash(obs)
    h2 = compute_observations_hash(obs)
    assert h1 == h2
    assert len(h1) == 64


# ---------------------------------------------------------------------------
# markdown 渲染：行动队列格式
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_report_to_markdown_action_queue_format() -> None:
    alert = _build_alert(
        alert_id="proj:s1:bug",
        priority="P1",
        severity="high",
        session_id="s1",
        title="白板 bug",
        recommendation="重启 server",
    )
    report = SiTianReport(
        report_id="test_001",
        generated_at="2026-05-16T10:00:00Z",
        summary="1 个 P1",
        model_name="test-model",
        errors=(),
        top_alerts=(alert,),
        projects=(),
    )
    md = report_to_markdown(report)
    assert "# 司天巡检 2026-05-16T10:00:00Z" in md
    assert "健康度" in md
    assert "P0 0 | P1 1 | P2 0" in md
    assert "## 当前最该处理" in md
    assert "[P1] 白板 bug" in md
    assert "重启 server" in md


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _build_alert(
    *,
    alert_id: str,
    priority: str = "P1",
    severity: str = "medium",
    session_id: str = "s1",
    title: str = "title",
    recommendation: str = "rec",
) -> SiTianAlert:
    return SiTianAlert(
        alert_id=alert_id,
        priority=priority,
        severity=severity,
        severity_reasons=(),
        title=title,
        display_message="msg",
        session_ref={"sessionId": session_id, "projectId": "/p", "projectName": "p"},
        evidence=(),
        recommendation=recommendation,
        dispatch={"targetThreadId": session_id, "instructionDraft": ""},
        dedupe_key=f"{alert_id}-dedupe",
    )
