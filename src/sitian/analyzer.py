"""
司天 LLM 分析层 V2——observations → LLM prompt → SiTianReport。

Role:
    接收扫描产出的 observations，调用 LLM 生成结构化报告（SiTianReport）。
    V2 schema 按 alert + session 维度产出问题清单，topAlerts 排序后供前端行动队列展示。
    核心流程 7 步：提取 observations → 脚本算 status → 构造 prompt →
    调 LLM → 规范化 alert → 排序 → 组装 report。

Owns:
    - LLM prompt 模板（system + user prompt 构造）
    - alert 规范化逻辑（alertId / dedupe_key / priority / severity）
    - topAlerts 三级排序（priority → severity → recency）
    - observations hash 增量判断

Does not own:
    - session 原始数据读取（session_reader.py）
    - 报告落盘（store.py）
    - Web 展示
    - 定时调度（service.py）

Called by:
    - service.py（_run_llm_analysis）

Key outputs:
    - sitian_analyze() → SiTianReport
    - report_to_markdown() → str
    - compute_observations_hash() → str

Change risks:
    - prompt 改动会影响报告稳定性和 LLM 调用成本
    - SiTianReport / SiTianAlert 字段变化会影响 store.py 和 Web 前端
    - 跨包引用 core.contracts（LLMProvider）—— analyzer 是 sitian 内唯一依赖 core 的模块
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.contracts import LLMProvider, LLMRequest
from core.message import Message
from sitian.config import SiTianAnalyzerConfig, SiTianInterestsConfig
from sitian.models import (
    SiTianAlert,
    SiTianObservation,
    SiTianProjectSnapshot,
    SiTianReport,
)

_log = logging.getLogger("sitian.analyzer")

_PROMPT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "templates" / "SITIAN_ANALYZER.md"
)

_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2}
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

# statusByRule 阈值（秒）：active ≤ 24h；idle ≤ 3d；超过 3d 为 dormant；无 session 为 empty
_STATUS_ACTIVE_SEC = 24 * 3600
_STATUS_IDLE_SEC = 3 * 24 * 3600

_TYPE_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ============================================================================
# 主入口
# ============================================================================


async def sitian_analyze(
    observations: tuple[SiTianObservation, ...],
    *,
    provider: LLMProvider,
    analyzer_config: SiTianAnalyzerConfig,
    interests_config: SiTianInterestsConfig,
    observed_at: str,
    sitian_root: Path | None = None,
) -> SiTianReport:
    """V2 入口：按 alert + session 维度产出 SiTianReport。

    LLM 失败时 logging.warning + 返回含 errors 的报告（保留 project snapshot 规则层数据）。
    ``sitian_root`` 用于审计日志落盘（full_log_enabled 时写到 ``sitian_root/full-log/``）。
    """
    report_id = _make_report_id(observed_at)
    model_name = analyzer_config.model_name or "unknown"

    project_obs = [obs for obs in observations if obs.entity_type == "project"]
    thread_obs = [obs for obs in observations if obs.entity_type == "thread"]

    if interests_config.projects:
        allowed = set(interests_config.projects)
        project_obs = [obs for obs in project_obs if obs.payload.get("cwd") in allowed]
        thread_obs = [obs for obs in thread_obs if obs.payload.get("cwd") in allowed]

    if not project_obs:
        return _empty_report(report_id, observed_at, model_name, summary="无可分析的项目数据")

    # 脚本算（确定性，不依赖 LLM）
    project_threads = _group_threads_by_project(project_obs, thread_obs)
    rule_metadata = {
        proj.entity_key: {
            "statusByRule": _compute_status_by_rule(proj.payload, observed_at),
            "sessionStats": _compute_session_stats(
                project_threads.get(proj.entity_key, []), proj.payload, observed_at
            ),
        }
        for proj in project_obs
    }

    # 构造 LLM input + system prompt
    user_data = _build_user_prompt_data(
        project_obs, project_threads, analyzer_config.max_context_chars
    )
    user_prompt = json.dumps(user_data, ensure_ascii=False, indent=2)
    system_prompt = _load_system_prompt(interests_config.focus)

    # 调 LLM
    response_content, response_usage, error_str = await _call_llm(
        provider, analyzer_config, system_prompt, user_prompt
    )
    if analyzer_config.full_log_enabled:
        _write_full_log(
            sitian_root=sitian_root,
            observed_at=observed_at,
            model_name=model_name,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_content=response_content,
            response_usage=response_usage,
            error=error_str,
        )

    if error_str is not None:
        return _error_report(
            report_id, observed_at, model_name, error_str, project_obs, rule_metadata
        )

    # 解析 LLM JSON
    content = response_content or ""
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError) as exc:
        _log.warning("sitian analyzer JSON parse failed: %s\nRaw: %s", exc, content[:500])
        return _error_report(
            report_id,
            observed_at,
            model_name,
            f"JSON parse failed: {exc}",
            project_obs,
            rule_metadata,
        )

    # 解析 → 规范化 → 排序
    try:
        raw_alerts = parsed.get("alerts", [])
        raw_projects = parsed.get("projects", [])
        summary = str(parsed.get("summary", ""))

        alerts = tuple(_normalize_alert(raw) for raw in raw_alerts if isinstance(raw, dict))
        alerts = _sort_top_alerts(alerts, project_threads)

        projects = _build_project_snapshots(project_obs, raw_projects, rule_metadata, alerts)
    except Exception as exc:
        _log.warning("sitian analyzer report mapping failed: %s", exc)
        return _error_report(
            report_id,
            observed_at,
            model_name,
            f"Report mapping failed: {exc}",
            project_obs,
            rule_metadata,
        )

    return SiTianReport(
        report_id=report_id,
        generated_at=observed_at,
        summary=summary,
        model_name=model_name,
        errors=(),
        top_alerts=alerts,
        projects=projects,
    )


# ============================================================================
# 脚本算（statusByRule + sessionStats）
# ============================================================================


def _compute_status_by_rule(project_payload: dict[str, Any], observed_at: str) -> str:
    """根据 lastModifiedAt 与 observed_at 差值判定 status。

    - 24h 内 → active
    - 24h~3d → idle
    - 超 3d → dormant
    - 无 session / lastModifiedAt 缺失 → empty
    """
    last_iso = project_payload.get("lastModifiedAt")
    if not last_iso:
        return "empty"
    delta = _delta_seconds(observed_at, str(last_iso))
    if delta is None:
        return "empty"
    if delta <= _STATUS_ACTIVE_SEC:
        return "active"
    if delta <= _STATUS_IDLE_SEC:
        return "idle"
    return "dormant"


def _compute_session_stats(
    threads: list[SiTianObservation],
    project_payload: dict[str, Any],
    observed_at: str,
) -> dict[str, int]:
    """按 lastModifiedAt 分桶计数 thread observations。"""
    total = int(project_payload.get("sessionCount", len(threads)))
    h1 = h24 = d3 = 0
    for t in threads:
        last_iso = t.payload.get("lastModifiedAt") or t.payload.get("lastModified")
        if not last_iso:
            continue
        delta = _delta_seconds(observed_at, str(last_iso))
        if delta is None:
            continue
        if delta <= 3600:
            h1 += 1
        if delta <= 24 * 3600:
            h24 += 1
        if delta <= 3 * 24 * 3600:
            d3 += 1
    return {
        "total": total,
        "activeWithin1h": h1,
        "activeWithin24h": h24,
        "activeWithin3d": d3,
    }


def _group_threads_by_project(
    project_obs: list[SiTianObservation],
    thread_obs: list[SiTianObservation],
) -> dict[str, list[SiTianObservation]]:
    """按 cwd 把 thread observations 关联到 project observation。"""
    project_cwd_to_id: dict[str, str] = {}
    for p in project_obs:
        cwd = p.payload.get("cwd")
        if isinstance(cwd, str):
            project_cwd_to_id[cwd] = p.entity_key

    grouped: dict[str, list[SiTianObservation]] = {p.entity_key: [] for p in project_obs}
    for t in thread_obs:
        cwd = t.payload.get("cwd")
        if not isinstance(cwd, str):
            continue
        proj_id = project_cwd_to_id.get(cwd)
        if proj_id is not None:
            grouped[proj_id].append(t)
    return grouped


# ============================================================================
# input 构造（按 session 展开）
# ============================================================================


def _build_user_prompt_data(
    project_obs: list[SiTianObservation],
    project_threads: dict[str, list[SiTianObservation]],
    max_context_chars: int,
) -> list[dict[str, Any]]:
    """按 lastModifiedAt 倒序，每个 project 含 sessions[] 含消息正文。"""
    sorted_obs = sorted(
        project_obs,
        key=lambda obs: obs.payload.get("lastModifiedAt", ""),
        reverse=True,
    )
    result: list[dict[str, Any]] = []
    total_chars = 0
    for obs in sorted_obs:
        p = obs.payload
        threads = project_threads.get(obs.entity_key, [])

        # 优先用 project.recentSessions（已聚合好消息），fallback 到 thread observations
        sessions_payload: list[dict[str, Any]]
        if p.get("recentSessions"):
            sessions_payload = list(p.get("recentSessions", []))
        else:
            sessions_payload = [
                {
                    "threadId": t.payload.get("threadId"),
                    "lastModified": t.payload.get("lastModifiedAt")
                    or t.payload.get("lastModified"),
                    "recentUserMessages": t.payload.get("recentUserMessages", []),
                    "recentAssistantMessages": t.payload.get("recentAssistantMessages", []),
                }
                for t in threads
            ]

        item = {
            "projectId": obs.entity_key,
            "projectName": p.get("displayName", "unknown"),
            "sessionCount": p.get("sessionCount", 0),
            "lastModifiedAt": p.get("lastModifiedAt", ""),
            "sessions": sessions_payload,
        }
        item_json = json.dumps(item, ensure_ascii=False)
        if total_chars + len(item_json) > max_context_chars and result:
            break
        result.append(item)
        total_chars += len(item_json)
    return result


# ============================================================================
# output 解析 + 规范化
# ============================================================================


def _normalize_type_slug(raw: str) -> str:
    """LLM 给的 typeSlug：小写 + 非字母数字替换为 -，去首尾 -。"""
    if not raw:
        return "unknown"
    cleaned = _TYPE_SLUG_RE.sub("-", raw.lower()).strip("-")
    return cleaned or "unknown"


def _project_short_name(project_id: str) -> str:
    return project_id.rsplit("/", 1)[-1] or project_id


def _normalize_alert(raw: dict[str, Any]) -> SiTianAlert:
    """LLM 原始 alert dict → SiTianAlert（生成 alertId / dedupe_key / dispatch 占位）。"""
    project_id = str(raw.get("projectId", ""))
    project_name = str(raw.get("projectName", ""))
    session_id = str(raw.get("sessionId", ""))
    type_slug = _normalize_type_slug(str(raw.get("typeSlug", "")))

    project_short = _project_short_name(project_id)
    session_short = session_id[:8] if session_id else "unknown"
    alert_id = f"{project_short}:{session_short}:{type_slug}"
    dedupe_key = f"{project_short}:{type_slug}"

    severity_reasons_raw = raw.get("severityReasons", [])
    severity_reasons: tuple[str, ...] = (
        tuple(str(r) for r in severity_reasons_raw)
        if isinstance(severity_reasons_raw, list)
        else ()
    )

    raw_evidence = raw.get("evidence", [])
    evidence: list[dict[str, str]] = []
    if isinstance(raw_evidence, list):
        for ev in raw_evidence:
            if isinstance(ev, dict):
                evidence.append(
                    {"threadId": str(ev.get("threadId", "")), "quote": str(ev.get("quote", ""))}
                )

    return SiTianAlert(
        alert_id=alert_id,
        priority=str(raw.get("priority", "P2")),
        severity=str(raw.get("severity", "low")),
        severity_reasons=severity_reasons,
        title=str(raw.get("title", "")),
        display_message=str(raw.get("displayMessage", "")),
        session_ref={
            "sessionId": session_id,
            "projectId": project_id,
            "projectName": project_name,
        },
        evidence=tuple(evidence),
        recommendation=str(raw.get("recommendation", "")),
        dispatch={
            "targetThreadId": session_id,
            "instructionDraft": str(raw.get("instructionDraft", "")),
        },
        dedupe_key=dedupe_key,
    )


def _sort_top_alerts(
    alerts: tuple[SiTianAlert, ...],
    project_threads: dict[str, list[SiTianObservation]],
) -> tuple[SiTianAlert, ...]:
    """三级排序：priority (P0>P1>P2) → severity (high>medium>low) → recency (新→旧)。"""
    # 建 sessionId → recency epoch 索引
    session_recency: dict[str, float] = {}
    for threads in project_threads.values():
        for t in threads:
            sid = str(t.payload.get("threadId", ""))
            iso = t.payload.get("lastModifiedAt") or t.payload.get("lastModified")
            if sid and iso:
                epoch = _iso_to_epoch(str(iso))
                if epoch is not None:
                    session_recency[sid] = epoch

    def sort_key(alert: SiTianAlert) -> tuple[int, int, float]:
        p_rank = _PRIORITY_RANK.get(alert.priority, 99)
        s_rank = _SEVERITY_RANK.get(alert.severity, 99)
        recency = session_recency.get(str(alert.session_ref.get("sessionId", "")), 0.0)
        # recency 取负数，让"越新越靠前"
        return (p_rank, s_rank, -recency)

    return tuple(sorted(alerts, key=sort_key))


def _build_project_snapshots(
    project_obs: list[SiTianObservation],
    raw_projects: list[Any],
    rule_metadata: dict[str, dict[str, Any]],
    alerts: tuple[SiTianAlert, ...],
) -> tuple[SiTianProjectSnapshot, ...]:
    """合并 LLM(narrative/statusReason) + 规则(statusByRule/sessionStats) + alert 关联。"""
    llm_by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_projects:
        if isinstance(raw, dict):
            pid = str(raw.get("projectId", ""))
            if pid:
                llm_by_id[pid] = raw

    alerts_by_project: dict[str, list[str]] = {}
    for alert in alerts:
        pid = str(alert.session_ref.get("projectId", ""))
        alerts_by_project.setdefault(pid, []).append(alert.alert_id)

    return tuple(
        SiTianProjectSnapshot(
            project_id=obs.entity_key,
            project_name=str(obs.payload.get("displayName", "unknown")),
            status_by_rule=str(rule_metadata.get(obs.entity_key, {}).get("statusByRule", "empty")),
            status_reason=str(llm_by_id.get(obs.entity_key, {}).get("statusReason", "")),
            narrative=str(llm_by_id.get(obs.entity_key, {}).get("narrative", "")),
            session_stats=dict(rule_metadata.get(obs.entity_key, {}).get("sessionStats", {})),
            alert_ids=tuple(alerts_by_project.get(obs.entity_key, [])),
        )
        for obs in project_obs
    )


# ============================================================================
# markdown 渲染（行动队列格式）
# ============================================================================


def report_to_markdown(report: SiTianReport) -> str:
    p_counts = {"P0": 0, "P1": 0, "P2": 0}
    for alert in report.top_alerts:
        if alert.priority in p_counts:
            p_counts[alert.priority] += 1

    # 健康度：100 - P0*30 - P1*10 - P2*3，下限 0
    health = max(0, 100 - p_counts["P0"] * 30 - p_counts["P1"] * 10 - p_counts["P2"] * 3)

    lines = [
        f"# 司天巡检 {report.generated_at}",
        "",
        f"- 报告 ID：{report.report_id}",
        f"- 模型：{report.model_name}",
        f"- 健康度 {health} | P0 {p_counts['P0']} | P1 {p_counts['P1']} | P2 {p_counts['P2']}",
        "",
        "## 总结",
        "",
        report.summary,
        "",
    ]

    if report.top_alerts:
        top = report.top_alerts[0]
        lines.extend(
            [
                "## 当前最该处理",
                "",
                f"**[{top.priority}] {top.title}**",
                "",
                f"- 项目：{top.session_ref.get('projectName', '')}",
                f"- 严重度：{top.severity}",
                f"- 建议：{top.recommendation}",
            ]
        )
        if top.evidence:
            lines.append("- 证据：")
            for ev in top.evidence:
                quote = ev.get("quote", "")
                lines.append(f"  - {quote[:120]}")
        lines.append("")

    rest_alerts = report.top_alerts[1:]
    if rest_alerts:
        for priority in ("P0", "P1", "P2"):
            same_priority = [a for a in rest_alerts if a.priority == priority]
            if not same_priority:
                continue
            lines.append(f"## {priority} 队列")
            lines.append("")
            for i, alert in enumerate(same_priority, start=1):
                lines.append(f"{i}. **{alert.title}** — {alert.recommendation}")
                lines.append(f"   ↳ 项目：{alert.session_ref.get('projectName', '')}")
            lines.append("")

    if report.projects:
        lines.append("## 项目状态")
        lines.append("")
        for proj in report.projects:
            stats = proj.session_stats
            lines.append(
                f"- **{proj.project_name}** "
                f"({proj.status_by_rule}) "
                f"sessions: total={stats.get('total', 0)} "
                f"1h={stats.get('activeWithin1h', 0)} "
                f"24h={stats.get('activeWithin24h', 0)} "
                f"3d={stats.get('activeWithin3d', 0)}"
            )
            if proj.narrative:
                lines.append(f"  - {proj.narrative}")
        lines.append("")

    if report.errors:
        lines.extend(["## 异常记录", ""])
        for error in report.errors:
            lines.append(f"- ⚠️ {error}")
        lines.append("")

    return "\n".join(lines)


# ============================================================================
# helper
# ============================================================================


def compute_observations_hash(observations: tuple[SiTianObservation, ...]) -> str:
    data = json.dumps(
        [obs.payload for obs in observations if obs.entity_type == "project"],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _make_report_id(observed_at: str) -> str:
    cleaned = observed_at.replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    return f"sitian_{cleaned}"


def _empty_report(report_id: str, generated_at: str, model_name: str, summary: str) -> SiTianReport:
    return SiTianReport(
        report_id=report_id,
        generated_at=generated_at,
        summary=summary,
        model_name=model_name,
        errors=(),
        top_alerts=(),
        projects=(),
    )


def _error_report(
    report_id: str,
    generated_at: str,
    model_name: str,
    error: str,
    project_obs: list[SiTianObservation],
    rule_metadata: dict[str, dict[str, Any]],
) -> SiTianReport:
    """LLM 失败时的 fallback：保留脚本算的 project snapshot，alerts 空。"""
    snapshots = tuple(
        SiTianProjectSnapshot(
            project_id=obs.entity_key,
            project_name=str(obs.payload.get("displayName", "unknown")),
            status_by_rule=str(rule_metadata.get(obs.entity_key, {}).get("statusByRule", "empty")),
            status_reason="(LLM 分析失败，仅返回规则化数据)",
            narrative="",
            session_stats=dict(rule_metadata.get(obs.entity_key, {}).get("sessionStats", {})),
            alert_ids=(),
        )
        for obs in project_obs
    )
    return SiTianReport(
        report_id=report_id,
        generated_at=generated_at,
        summary="LLM 分析失败，请查看日志。已保留规则层项目快照。",
        model_name=model_name,
        errors=(error,),
        top_alerts=(),
        projects=snapshots,
    )


async def _call_llm(
    provider: LLMProvider,
    analyzer_config: SiTianAnalyzerConfig,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str | None, dict[str, Any], str | None]:
    try:
        request = LLMRequest(
            model=analyzer_config.model_name,
            messages=(
                Message.system(system_prompt),
                Message.user(user_prompt),
            ),
            temperature=analyzer_config.temperature,
            max_tokens=analyzer_config.max_tokens,
            timeout_seconds=float(analyzer_config.timeout),
        )
        response = await provider.complete(request)
        content = response.message.content or ""
        usage = getattr(response, "usage", {}) or {}
        return content, usage, None
    except Exception as exc:
        _log.warning("sitian analyzer LLM call failed: %s", exc)
        return None, {}, f"LLM call failed: {exc}"


def _load_system_prompt(interests_focus: str) -> str:
    try:
        template = _PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("failed to read sitian analyzer prompt template: %s", exc)
        template = (
            "你是司天，工作区观察者。分析项目进展，返回 JSON 格式报告。\n\n{{interests_focus}}"
        )
    return template.replace("{{interests_focus}}", interests_focus or "（未配置用户关注点）")


def _delta_seconds(observed_at: str, target_iso: str) -> float | None:
    """observed_at - target_iso 的秒差（>=0）。解析失败返回 None。"""
    try:
        observed_dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        target_dt = datetime.fromisoformat(target_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (observed_dt - target_dt).total_seconds()
    return max(0.0, delta)


def _iso_to_epoch(iso: str) -> float | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.timestamp()


def _write_full_log(
    *,
    sitian_root: Path | None,
    observed_at: str,
    model_name: str,
    system_prompt: str,
    user_prompt: str,
    response_content: str | None,
    response_usage: dict[str, Any],
    error: str | None,
) -> None:
    if sitian_root is None:
        _log.warning("full_log_enabled but sitian_root is None, skip audit log")
        return
    log_dir = sitian_root / "full-log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
        log_entry = {
            "timestamp": observed_at,
            "modelName": model_name,
            "request": {
                "systemPrompt": system_prompt,
                "userPrompt": user_prompt,
            },
            "response": {
                "content": response_content,
                "usage": response_usage,
            },
            "error": error,
        }
        log_path = log_dir / f"analyzer-{ts}.json"
        fd, tmp = tempfile.mkstemp(dir=str(log_dir), suffix=".tmp", prefix=".audit_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(log_entry, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, log_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        _log.info("audit log written: %s", log_path)
    except Exception as exc:
        _log.warning("failed to write audit log: %s", exc)


__all__ = ["compute_observations_hash", "report_to_markdown", "sitian_analyze"]
