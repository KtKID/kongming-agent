"""司天 LLM 分析层。

读取 observations 中的项目数据，构造 prompt 喂给 LLM，解析 JSON 返回 SiTianReport。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.contracts import LLMProvider, LLMRequest
from core.message import Message
from sitian.config import SiTianAnalyzerConfig, SiTianInterestsConfig
from sitian.models import SiTianObservation, SiTianReport, SiTianReportItem

_log = logging.getLogger("sitian.analyzer")

_PROMPT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "templates" / "SITIAN_ANALYZER.md"
)


async def sitian_analyze(
    observations: tuple[SiTianObservation, ...],
    *,
    provider: LLMProvider,
    analyzer_config: SiTianAnalyzerConfig,
    interests_config: SiTianInterestsConfig,
    observed_at: str,
    sitian_root: Path | None = None,
) -> SiTianReport:
    """用 LLM 分析 observations，返回 SiTianReport。

    LLM 失败时 logging.warning + 返回含 errors 的空报告（不抛异常）。
    ``sitian_root`` 用于审计日志落盘（full_log_enabled 时写到 ``sitian_root/full-log/``）。
    """
    report_id = _make_report_id(observed_at)
    model_name = analyzer_config.model_name or "unknown"

    project_obs = [obs for obs in observations if obs.entity_type == "project"]

    if interests_config.projects:
        allowed = set(interests_config.projects)
        project_obs = [obs for obs in project_obs if obs.payload.get("cwd") in allowed]

    if not project_obs:
        return SiTianReport(
            report_id=report_id,
            generated_at=observed_at,
            summary="无可分析的项目数据",
            items=(),
            model_name=model_name,
            errors=(),
        )

    user_data = _build_user_prompt_data(project_obs, analyzer_config.max_context_chars)
    user_prompt = json.dumps(user_data, ensure_ascii=False, indent=2)
    system_prompt = _load_system_prompt(interests_config.focus)

    response_content: str | None = None
    response_usage: dict[str, Any] = {}
    error_str: str | None = None

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
        response_content = response.message.content or ""
        response_usage = getattr(response, "usage", {}) or {}
    except Exception as exc:
        error_str = f"LLM call failed: {exc}"
        _log.warning("sitian analyzer LLM call failed: %s", exc)

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
        return _error_report(report_id, observed_at, model_name, error_str)

    content = response_content or ""
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError) as exc:
        _log.warning("sitian analyzer JSON parse failed: %s\nRaw: %s", exc, content[:500])
        return _error_report(report_id, observed_at, model_name, f"JSON parse failed: {exc}")

    try:
        items = tuple(SiTianReportItem.from_dict(item) for item in parsed.get("items", []))
        summary = str(parsed.get("summary", ""))
    except Exception as exc:
        _log.warning("sitian analyzer report mapping failed: %s", exc)
        return _error_report(report_id, observed_at, model_name, f"Report mapping failed: {exc}")

    return SiTianReport(
        report_id=report_id,
        generated_at=observed_at,
        summary=summary,
        items=items,
        model_name=model_name,
        errors=(),
    )


def compute_observations_hash(observations: tuple[SiTianObservation, ...]) -> str:
    data = json.dumps(
        [obs.payload for obs in observations if obs.entity_type == "project"],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def report_to_markdown(report: SiTianReport) -> str:
    lines = [
        "# 司天分析报告",
        "",
        f"- 报告 ID：{report.report_id}",
        f"- 生成时间：{report.generated_at}",
        f"- 模型：{report.model_name}",
        "",
        "## 总结",
        "",
        report.summary,
        "",
    ]

    if report.items:
        lines.append("## 项目详情")
        lines.append("")
        for item in report.items:
            emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(item.severity, "⚪")
            lines.extend(
                [
                    f"### {emoji} {item.project_name}",
                    "",
                    f"- 状态：{item.status}",
                    f"- 优先级：{item.severity}",
                    f"- 活跃度：{item.recent_activity}",
                    "",
                    item.narrative,
                    "",
                ]
            )
            if item.next_actions:
                lines.append("**下一步建议：**")
                for action in item.next_actions:
                    lines.append(f"- {action}")
                lines.append("")

    if report.errors:
        lines.extend(["## 异常记录", ""])
        for error in report.errors:
            lines.append(f"- ⚠️ {error}")
        lines.append("")

    return "\n".join(lines)


def _make_report_id(observed_at: str) -> str:
    cleaned = observed_at.replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    return f"sitian_{cleaned}"


def _error_report(report_id: str, generated_at: str, model_name: str, error: str) -> SiTianReport:
    return SiTianReport(
        report_id=report_id,
        generated_at=generated_at,
        summary="LLM 分析失败，请查看日志",
        items=(),
        model_name=model_name,
        errors=(error,),
    )


def _build_user_prompt_data(
    project_obs: list[SiTianObservation],
    max_context_chars: int,
) -> list[dict[str, Any]]:
    sorted_obs = sorted(
        project_obs,
        key=lambda obs: obs.payload.get("lastModifiedAt", ""),
        reverse=True,
    )
    result: list[dict[str, Any]] = []
    total_chars = 0
    for obs in sorted_obs:
        p = obs.payload
        item = {
            "projectId": obs.entity_key,
            "projectName": p.get("displayName", "unknown"),
            "sessionCount": p.get("sessionCount", 0),
            "lastModifiedAt": p.get("lastModifiedAt", ""),
            "recentSessions": p.get("recentSessions", []),
        }
        item_json = json.dumps(item, ensure_ascii=False)
        if total_chars + len(item_json) > max_context_chars and result:
            break
        result.append(item)
        total_chars += len(item_json)
    return result


def _load_system_prompt(interests_focus: str) -> str:
    try:
        template = _PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("failed to read sitian analyzer prompt template: %s", exc)
        template = (
            "你是司天，工作区观察者。分析项目进展，返回 JSON 格式报告。\n\n{{interests_focus}}"
        )
    return template.replace("{{interests_focus}}", interests_focus or "（未配置用户关注点）")


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
            with __import__("contextlib").suppress(OSError):
                os.unlink(tmp)
            raise
        _log.info("audit log written: %s", log_path)
    except Exception as exc:
        _log.warning("failed to write audit log: %s", exc)


__all__ = ["compute_observations_hash", "report_to_markdown", "sitian_analyze"]
