"""unit：prompting sitian context build_sitian_context_text 全面测试。

覆盖场景：
- 正常 3 项 work_items → 表格 + freshness
- 文件缺失 → None
- JSON 损坏 → None
- 空 work_items → None
- 截断超 15 项 → "...及 N 个更多项目"
- freshness 超 1h → "可能已过期"
- freshness 新鲜 → 无"可能已过期"
- latest_summary.md 存在 / 不存在
- blockers 非空 → "#### 阻塞项"
- blockers 为空 → 无"#### 阻塞项"
- risks 非空 → "#### 风险项"
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import prompting.sitian_context as sitian_context_mod
from prompting.sitian_context import MAX_ITEMS_PER_CHANNEL, build_sitian_context_text

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


def _make_state(
    *,
    work_items: list[dict[str, Any]] | None = None,
    updated_at: str = "2026-05-10T11:55:00Z",
    blockers: list[dict[str, Any]] | None = None,
    risks: list[dict[str, Any]] | None = None,
    sources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 workspace_state.json 的 dict。"""
    if work_items is None:
        work_items = [
            {
                "id": f"item-{i}",
                "title": f"项目{chr(65 + i)}",
                "status": "active",
                "priority": "low",
                "blockers": [],
                "nextActions": ["查看最新线程"],
                "risks": [],
                "updatedAt": "2026-05-10T11:50:00Z",
            }
            for i in range(3)
        ]
    return {
        "version": "v1",
        "updatedAt": updated_at,
        "sources": sources or {"total": 3, "active": 3},
        "workItems": work_items,
        "blockers": blockers or [],
        "risks": risks or [],
    }


def _write_state(root: Path, state: dict[str, Any], channel: str = "claude") -> Path:
    """写 workspace_state.json 到 root/<channel>/ 下。

    root 是 sitian_root（频道父目录），channel 默认 "claude"。
    """
    channel_dir = root / channel
    channel_dir.mkdir(parents=True, exist_ok=True)
    p = channel_dir / "workspace_state.json"
    p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return p


def _patch_now():
    """Patch datetime.now inside sitian_context to return _FIXED_NOW."""
    original_datetime = datetime

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return _FIXED_NOW
            return _FIXED_NOW.replace(tzinfo=None)

        @classmethod
        def fromisoformat(cls, s: str) -> datetime:
            return original_datetime.fromisoformat(s)

    return patch.object(sitian_context_mod, "datetime", _FakeDatetime)


# ---------------------------------------------------------------------------
# 1. 正常：3 个 work_items → 表格 + freshness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_normal_3_items(tmp_path: Path) -> None:
    state = _make_state()
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "| # | 项目 |" in result
    assert "项目A" in result
    assert "项目B" in result
    assert "项目C" in result
    # 3 个项目应该有 3 行数据行（| 1 |、| 2 |、| 3 |）
    assert "| 1 |" in result
    assert "| 2 |" in result
    assert "| 3 |" in result
    # freshness: 5 分钟前，不含"可能已过期"
    assert "5 分钟前" in result
    assert "可能已过期" not in result


# ---------------------------------------------------------------------------
# 2. 文件缺失 → None
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_directory(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does_not_exist"
    result = build_sitian_context_text(nonexistent)
    assert result is None


@pytest.mark.unit
def test_missing_state_file(tmp_path: Path) -> None:
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    result = build_sitian_context_text(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# 3. JSON 损坏 → None
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_corrupted_json(tmp_path: Path) -> None:
    channel = tmp_path / "claude"
    channel.mkdir(parents=True, exist_ok=True)
    (channel / "workspace_state.json").write_text("{broken json!!", encoding="utf-8")
    result = build_sitian_context_text(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# 4. 空 work_items → None
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_work_items(tmp_path: Path) -> None:
    state = _make_state(work_items=[])
    _write_state(tmp_path, state)
    result = build_sitian_context_text(tmp_path)
    assert result is None


@pytest.mark.unit
def test_null_work_items(tmp_path: Path) -> None:
    """workItems 为 null（JSON null）→ None。"""
    state = _make_state()
    state["workItems"] = None
    _write_state(tmp_path, state)
    result = build_sitian_context_text(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# 5. 截断：超 15 项 → "...及 N 个更多项目"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_truncation_over_max_items(tmp_path: Path) -> None:
    total = MAX_ITEMS_PER_CHANNEL + 5  # 20 items
    items = [
        {
            "id": f"item-{i}",
            "title": f"项目{i}",
            "status": "active",
            "priority": "medium",
            "blockers": [],
            "nextActions": [],
            "risks": [],
            "updatedAt": "2026-05-10T11:50:00Z",
        }
        for i in range(total)
    ]
    state = _make_state(work_items=items)
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    overflow = total - MAX_ITEMS_PER_CHANNEL
    assert f"...及 {overflow} 个更多项目" in result
    # 前 15 项存在
    assert f"| {MAX_ITEMS_PER_CHANNEL} |" in result
    # 第 16 项不作为表格行出现
    assert f"| {MAX_ITEMS_PER_CHANNEL + 1} |" not in result


@pytest.mark.unit
def test_exact_max_items_no_truncation(tmp_path: Path) -> None:
    """正好 15 项不应出现截断提示。"""
    items = [
        {
            "id": f"item-{i}",
            "title": f"项目{i}",
            "status": "active",
            "priority": "medium",
            "blockers": [],
            "nextActions": [],
            "risks": [],
            "updatedAt": "2026-05-10T11:50:00Z",
        }
        for i in range(MAX_ITEMS_PER_CHANNEL)
    ]
    state = _make_state(work_items=items)
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "更多项目" not in result


# ---------------------------------------------------------------------------
# 6. freshness 超 1h → "可能已过期"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stale_freshness(tmp_path: Path) -> None:
    # updatedAt 在 2 小时前
    stale_time = (_FIXED_NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = _make_state(updated_at=stale_time)
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "可能已过期" in result
    assert "2 小时前" in result


# ---------------------------------------------------------------------------
# 7. freshness 新鲜（10 分钟前） → 无"可能已过期"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fresh_data(tmp_path: Path) -> None:
    fresh_time = (_FIXED_NOW - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = _make_state(updated_at=fresh_time)
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "10 分钟前" in result
    # 顶层 freshness 行不含"可能已过期"
    for line in result.splitlines():
        if "数据采集于" in line:
            assert "可能已过期" not in line
            break


# ---------------------------------------------------------------------------
# 8. latest_summary.md 存在 → "#### 最近摘要"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_summary_present(tmp_path: Path) -> None:
    state = _make_state()
    _write_state(tmp_path, state)
    (tmp_path / "claude" / "latest_summary.md").write_text("这是一段摘要文本。", encoding="utf-8")

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "#### 最近摘要" in result
    assert "这是一段摘要文本。" in result


@pytest.mark.unit
def test_summary_empty_file(tmp_path: Path) -> None:
    """summary 文件存在但内容为空 → 不应出现"#### 最近摘要"。"""
    state = _make_state()
    _write_state(tmp_path, state)
    (tmp_path / "claude" / "latest_summary.md").write_text("", encoding="utf-8")

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "#### 最近摘要" not in result


# ---------------------------------------------------------------------------
# 9. latest_summary.md 不存在 → 无"#### 最近摘要"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_summary_absent(tmp_path: Path) -> None:
    state = _make_state()
    _write_state(tmp_path, state)
    # 不创建 latest_summary.md

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "#### 最近摘要" not in result


# ---------------------------------------------------------------------------
# 10. blockers 非空 → "#### 阻塞项"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_blockers_present(tmp_path: Path) -> None:
    state = _make_state(
        blockers=[
            {"workItemId": "item-0", "summary": "依赖服务挂了"},
            {"workItemId": "item-1", "summary": "等待审批"},
        ]
    )
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "#### 阻塞项" in result
    assert "item-0: 依赖服务挂了" in result
    assert "item-1: 等待审批" in result


# ---------------------------------------------------------------------------
# 11. blockers 为空 → 无"#### 阻塞项"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_blockers_empty(tmp_path: Path) -> None:
    state = _make_state(blockers=[])
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "#### 阻塞项" not in result


# ---------------------------------------------------------------------------
# 12. risks 非空 → "#### 风险项"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_risks_present(tmp_path: Path) -> None:
    state = _make_state(
        risks=[
            {"category": "安全", "summary": "密钥即将过期"},
        ]
    )
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "#### 风险项" in result
    assert "安全: 密钥即将过期" in result


@pytest.mark.unit
def test_risks_empty(tmp_path: Path) -> None:
    state = _make_state(risks=[])
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "#### 风险项" not in result


# ---------------------------------------------------------------------------
# 补充：状态翻译 + nextActions 截断 + item-level freshness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_translation(tmp_path: Path) -> None:
    """各 status 翻译为中文。"""
    items = [
        {
            "id": f"s-{s}",
            "title": f"测试{s}",
            "status": s,
            "priority": "high",
            "blockers": [],
            "nextActions": [],
            "risks": [],
            "updatedAt": "2026-05-10T11:50:00Z",
        }
        for s in ("active", "blocked", "waiting", "done", "uncertain")
    ]
    state = _make_state(work_items=items)
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "活跃" in result
    assert "阻塞" in result
    assert "等待中" in result
    assert "完成" in result
    assert "未知" in result


@pytest.mark.unit
def test_item_level_stale(tmp_path: Path) -> None:
    """单个 work item 的 updatedAt 超 1h 也会标"可能已过期"。"""
    stale_item_time = (_FIXED_NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = [
        {
            "id": "old-item",
            "title": "过期项目",
            "status": "active",
            "priority": "high",
            "blockers": [],
            "nextActions": ["做点什么"],
            "risks": [],
            "updatedAt": stale_item_time,
        },
    ]
    # 顶层 updatedAt 新鲜，但 item 本身过期
    fresh_time = (_FIXED_NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = _make_state(work_items=items, updated_at=fresh_time)
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    # 找到包含"过期项目"的行，确认该行有"可能已过期"
    for line in result.splitlines():
        if "过期项目" in line:
            assert "可能已过期" in line
            break
    else:
        pytest.fail("未找到包含'过期项目'的行")


@pytest.mark.unit
def test_next_action_truncation(tmp_path: Path) -> None:
    """nextActions 第一项超 30 字符时应被截断。"""
    long_action = "这是一个非常非常非常非常非常非常非常非常非常非常非常非常长的下一步动作描述文本"
    assert len(long_action) > 30
    items = [
        {
            "id": "trunc-item",
            "title": "截断测试",
            "status": "active",
            "priority": "low",
            "blockers": [],
            "nextActions": [long_action],
            "risks": [],
            "updatedAt": "2026-05-10T11:55:00Z",
        },
    ]
    state = _make_state(work_items=items)
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    # 完整文本不应出现
    assert long_action not in result
    # 截断标记 "…" 应出现
    assert "…" in result


@pytest.mark.unit
def test_no_next_actions_fallback(tmp_path: Path) -> None:
    """nextActions 为空时 → 显示"同步最新状态"。"""
    items = [
        {
            "id": "no-action",
            "title": "无动作",
            "status": "active",
            "priority": "low",
            "blockers": [],
            "nextActions": [],
            "risks": [],
            "updatedAt": "2026-05-10T11:55:00Z",
        },
    ]
    state = _make_state(work_items=items)
    _write_state(tmp_path, state)

    with _patch_now():
        result = build_sitian_context_text(tmp_path)

    assert result is not None
    assert "同步最新状态" in result
