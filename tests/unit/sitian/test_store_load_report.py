"""``SiTianRecordsStore.load_sitian_report`` 单元测试（sitian-web-report-dialog #1）。

覆盖：

1. 报告文件存在且合法 JSON → 返回 dict（与写入内容等值）
2. 报告文件不存在 → 返回 ``None``
3. 报告文件存在但 JSON 损坏 → 抛 :class:`json.JSONDecodeError`
4. 报告 JSON 顶层非 object（如 list） → 抛 :class:`ValueError`
5. save → load 往返一致（与 :meth:`save_sitian_report` 对称）

设计要点：
    - 用 ``tmp_path`` fixture 直接实例化 ``SiTianRecordsStore(tmp_path)``，
      跳过 :func:`resolve_sitian_root` 的全局 home 依赖，测试间天然隔离
    - 用 ``ensure_layout`` 显式建目录，再手写 ``sitian_report.json``，模拟
      "保存后又被外部破坏 / 删除"等真实事故
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sitian.store import SiTianRecordsStore


@pytest.mark.asyncio
async def test_load_report_returns_dict_when_present(tmp_path: Path) -> None:
    """文件存在且合法 JSON → 返回 dict，字段原样。"""
    store = SiTianRecordsStore(tmp_path)
    payload: dict[str, object] = {
        "reportId": "rep-1",
        "generatedAt": "2026-05-16T12:00:00Z",
        "modelName": "fake-model",
        "summary": "all good",
        "errors": [],
        "topAlerts": [],
        "projects": [],
    }
    saved_path = await store.save_sitian_report(payload)
    assert saved_path.exists()

    loaded = await store.load_sitian_report()
    assert loaded == payload


@pytest.mark.asyncio
async def test_load_report_returns_none_when_missing(tmp_path: Path) -> None:
    """文件不存在 → 返回 None；不抛异常。"""
    store = SiTianRecordsStore(tmp_path)
    # 显式不调 ensure_layout/save_*，验证 store 对"裸目录"也健壮
    loaded = await store.load_sitian_report()
    assert loaded is None


@pytest.mark.asyncio
async def test_load_report_returns_none_when_layout_exists_but_no_report(
    tmp_path: Path,
) -> None:
    """layout 已建但报告文件缺失 → None。"""
    store = SiTianRecordsStore(tmp_path)
    await store.ensure_layout()
    # 确认根目录创建好了但没 sitian_report.json
    assert (tmp_path / "sitian_report.json").exists() is False

    loaded = await store.load_sitian_report()
    assert loaded is None


@pytest.mark.asyncio
async def test_load_report_raises_on_corrupted_json(tmp_path: Path) -> None:
    """文件存在但内容不是合法 JSON → 抛 JSONDecodeError。

    上层 web 路由层据此翻译为 ``500 report_corrupted``，所以这里不能
    静默吞或返 None——必须把损坏信号往上传。
    """
    store = SiTianRecordsStore(tmp_path)
    await store.ensure_layout()
    report_path = tmp_path / "sitian_report.json"
    report_path.write_text("{not: valid json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        await store.load_sitian_report()


@pytest.mark.asyncio
async def test_load_report_raises_when_top_level_not_object(tmp_path: Path) -> None:
    """JSON 合法但顶层不是 object（list / scalar） → 抛 ValueError。

    报告 schema 约定顶层是 dict（reportId / generatedAt / ...）。
    一旦顶层退化为 list 或 scalar，前端字段全部 undefined，按损坏处理
    更安全，与 ``500 report_corrupted`` 路径合并。
    """
    store = SiTianRecordsStore(tmp_path)
    await store.ensure_layout()
    report_path = tmp_path / "sitian_report.json"
    report_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    with pytest.raises(ValueError):
        await store.load_sitian_report()


@pytest.mark.asyncio
async def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    """save → load 往返：字段、顺序无关、嵌套结构一致。"""
    store = SiTianRecordsStore(tmp_path)
    payload: dict[str, object] = {
        "reportId": "rep-2",
        "topAlerts": [
            {
                "alertId": "a-1",
                "priority": "P0",
                "severity": "high",
                "title": "test",
            }
        ],
        "projects": [
            {
                "projectId": "p-1",
                "sessionStats": {
                    "total": 3,
                    "activeWithin1h": 1,
                    "activeWithin24h": 2,
                    "activeWithin3d": 3,
                },
            }
        ],
    }
    await store.save_sitian_report(payload)
    loaded = await store.load_sitian_report()
    assert loaded == payload
    # 嵌套结构未被破坏
    assert isinstance(loaded, dict)
    assert loaded["topAlerts"][0]["alertId"] == "a-1"  # type: ignore[index]
    assert loaded["projects"][0]["sessionStats"]["total"] == 3  # type: ignore[index]
