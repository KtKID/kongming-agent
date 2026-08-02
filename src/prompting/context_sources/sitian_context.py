from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_ITEMS_PER_CHANNEL = 15

_STATUS_ZH: dict[str, str] = {
    "active": "活跃",
    "blocked": "阻塞",
    "waiting": "等待中",
    "done": "完成",
    "uncertain": "未知",
}


def _relative_time(iso_str: str, now: datetime) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return "未知"
    delta = now - dt
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "刚刚"
    if total_seconds < 60:
        return f"{total_seconds} 秒前"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = total_seconds // 3600
    if hours < 24:
        return f"{hours} 小时前"
    days = total_seconds // 86400
    return f"{days} 天前"


def _is_stale(iso_str: str, now: datetime) -> bool:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return True
    return (now - dt).total_seconds() > 3600


def _truncate(text: str, max_len: int = 30) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def build_sitian_context_text(sitian_root: Path) -> str | None:
    """扫描 sitian_root 下所有频道子目录，聚合为 system prompt 文本。

    目录结构：sitian_root/<channel>/workspace_state.json
    每个子目录是一个频道（如 claude、codex、general）。
    """
    if not sitian_root.is_dir():
        return None

    now = datetime.now(UTC)
    channel_sections: list[str] = []

    for channel_dir in sorted(sitian_root.iterdir()):
        if not channel_dir.is_dir():
            continue
        section = _build_channel_section(channel_dir, channel_dir.name, now)
        if section:
            channel_sections.append(section)

    if not channel_sections:
        return None

    lines: list[str] = [
        "## 工作区态势（司天观察）",
        "",
        f"频道数：{len(channel_sections)}",
        "",
    ]
    lines.append("\n\n".join(channel_sections))
    lines.append("")
    lines.append("以上数据由司天自动扫描产出，可能存在延迟。如需最新状态请询问用户手动触发扫描。")

    return "\n".join(lines)


def _build_channel_section(channel_dir: Path, channel_name: str, now: datetime) -> str | None:
    state_path = channel_dir / "workspace_state.json"
    summary_path = channel_dir / "latest_summary.md"
    analysis_path = channel_dir / "latest_analysis.md"

    try:
        raw = state_path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None

    try:
        state: dict[str, Any] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None

    work_items: list[dict[str, Any]] = state.get("workItems") or []
    if not work_items:
        return None

    updated_at: str = state.get("updatedAt", "")
    sources: dict[str, Any] = state.get("sources") or {}
    sources_total = sources.get("total", "?")
    sources_active = sources.get("active", "?")
    blockers: list[dict[str, Any]] = state.get("blockers") or []
    risks: list[dict[str, Any]] = state.get("risks") or []

    freshness = _relative_time(updated_at, now)
    if _is_stale(updated_at, now):
        freshness += "（可能已过期）"

    lines: list[str] = []
    lines.append(f"### 频道：{channel_name}")
    lines.append("")
    lines.append(
        f"数据采集于 {updated_at}（{freshness}）"
        f"\n观察源：{sources_active}/{sources_total} 活跃，{len(work_items)} 个项目"
    )
    lines.append("")

    lines.append("| # | 项目 | 状态 | 优先级 | 最近活跃 | 下一步 |")
    lines.append("|---|------|------|--------|---------|--------|")

    display_items = work_items[:MAX_ITEMS_PER_CHANNEL]
    overflow = len(work_items) - MAX_ITEMS_PER_CHANNEL

    for i, item in enumerate(display_items, 1):
        title = item.get("title", item.get("id", "?"))
        status_raw = item.get("status", "uncertain")
        status = _STATUS_ZH.get(status_raw, status_raw)
        priority = item.get("priority", "?")
        item_updated = item.get("updatedAt", "")
        item_freshness = _relative_time(item_updated, now)
        if _is_stale(item_updated, now):
            item_freshness += "（可能已过期）"
        next_actions: list[str] = item.get("nextActions") or []
        next_step = _truncate(next_actions[0]) if next_actions else "同步最新状态"
        lines.append(f"| {i} | {title} | {status} | {priority} | {item_freshness} | {next_step} |")

    if overflow > 0:
        lines.append("")
        lines.append(f"...及 {overflow} 个更多项目")

    if blockers:
        lines.append("")
        lines.append("#### 阻塞项")
        for b in blockers:
            wid = b.get("workItemId", "?")
            summary = b.get("summary", "无描述")
            lines.append(f"- {wid}: {summary}")

    if risks:
        lines.append("")
        lines.append("#### 风险项")
        for r in risks:
            category = r.get("category", "?")
            summary = r.get("summary", "无描述")
            lines.append(f"- {category}: {summary}")

    if summary_path.is_file():
        try:
            summary_text = summary_path.read_text(encoding="utf-8").strip()
            if summary_text:
                lines.append("")
                lines.append("#### 最近摘要")
                lines.append(summary_text)
        except OSError:
            pass

    if analysis_path.is_file():
        try:
            analysis_text = analysis_path.read_text(encoding="utf-8").strip()
            if analysis_text:
                lines.append("")
                lines.append("#### 最近分析")
                lines.append(analysis_text)
        except OSError:
            pass

    return "\n".join(lines)


__all__ = ["build_sitian_context_text"]
