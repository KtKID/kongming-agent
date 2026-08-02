"""``src/sitian/service.py::SiTianRunOnce`` 耗时审计字段测试。

覆盖：
1. scan snapshot 含 4 个新字段：startedAt / finishedAt / durationMs / analyzerDurationMs
2. 不传 llm_provider → analyzerDurationMs is None
3. 传 llm_provider 且 analyzer.enabled → analyzerDurationMs >= 0
4. observedAt 字段保留（向后兼容）
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from infrastructure.config.models import Config, ModelSelectionConfig
from sitian.config import SiTianAnalyzerConfig, SiTianConfig, SiTianSourceConfig
from sitian.store import SiTianRecordsStore


def _build_cfg(*, source: SiTianSourceConfig, analyzer_enabled: bool = False) -> Config:
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        sitian=SiTianConfig(
            default_scan_interval_sec=60,
            idle_sleep_sec=2,
            sources=[source],
            analyzer=SiTianAnalyzerConfig(
                enabled=analyzer_enabled,
                preset_id="local-gemma-4-e4b-it",
                skip_if_unchanged=False,
            ),
        ),
    )


def _load_scan_snapshot(store: SiTianRecordsStore) -> dict:
    scans_dir = store.root_dir / "scans"
    files = sorted(scans_dir.glob("scan-*.json"))
    assert files, f"no scan snapshot under {scans_dir}"
    return json.loads(files[-1].read_text(encoding="utf-8"))


@pytest.mark.unit
def test_scan_snapshot_contains_timing_fields_without_analyzer(tmp_path: Path) -> None:
    """不传 llm_provider → 4 字段都在，analyzerDurationMs 为 None。"""
    from sitian.service import SiTianRunOnce

    project_dir = tmp_path / "channel"
    project_dir.mkdir()
    (project_dir / "task.md").write_text("hi", encoding="utf-8")

    cfg = _build_cfg(
        source=SiTianSourceConfig(
            id="ch",
            kind="generic_channel",
            path=str(project_dir),
            scan_interval_sec=30,
        ),
    )
    store = SiTianRecordsStore(tmp_path / "records")

    asyncio.run(
        SiTianRunOnce(
            cfg,
            store=store,
            now=datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC),
        )
    )

    snap = _load_scan_snapshot(store)
    assert snap["startedAt"] == "2026-05-09T10:00:00Z"
    assert snap["observedAt"] == snap["startedAt"], "observedAt 必须保留向后兼容"
    assert isinstance(snap["finishedAt"], str) and snap["finishedAt"].endswith("Z")
    assert isinstance(snap["durationMs"], int) and snap["durationMs"] >= 0
    assert snap["analyzerDurationMs"] is None


@pytest.mark.unit
def test_scan_snapshot_records_analyzer_duration_when_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """传 provider + analyzer.enabled → analyzerDurationMs 为非负 int（mock 内部 LLM 调用）。"""
    import sitian.service as svc

    # mock 内部 _run_llm_analysis 直接返回固定耗时，避免真调 LLM
    async def _fake_analysis(*, records, provider, sitian_cfg, observations, observed_at) -> int:
        return 12345

    monkeypatch.setattr(svc, "_run_llm_analysis", _fake_analysis)

    project_dir = tmp_path / "channel"
    project_dir.mkdir()
    (project_dir / "task.md").write_text("hi", encoding="utf-8")

    cfg = _build_cfg(
        source=SiTianSourceConfig(
            id="ch",
            kind="generic_channel",
            path=str(project_dir),
            scan_interval_sec=30,
        ),
        analyzer_enabled=True,
    )
    store = SiTianRecordsStore(tmp_path / "records")

    # 传一个非空占位 provider（monkeypatched 的 _run_llm_analysis 不会真用它）
    fake_provider: object = object()
    asyncio.run(
        svc.SiTianRunOnce(
            cfg,
            store=store,
            now=datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC),
            llm_provider=fake_provider,  # type: ignore[arg-type]
        )
    )

    snap = _load_scan_snapshot(store)
    assert snap["analyzerDurationMs"] == 12345
    assert snap["durationMs"] >= 0
    assert snap["startedAt"] == "2026-05-09T10:00:00Z"
    assert snap["finishedAt"].endswith("Z")


@pytest.mark.unit
def test_scan_snapshot_finished_at_present_on_source_failure(
    tmp_path: Path,
) -> None:
    """source 扫描挂了，scan snapshot 仍要写 finishedAt / durationMs（审计要齐全）。"""
    from sitian.service import SiTianRunOnce

    cfg = _build_cfg(
        source=SiTianSourceConfig(
            id="bad",
            kind="generic_channel",
            path="/nonexistent/path/__must__not__exist__",
            scan_interval_sec=30,
        ),
    )
    store = SiTianRecordsStore(tmp_path / "records")

    asyncio.run(
        SiTianRunOnce(
            cfg,
            store=store,
            now=datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC),
        )
    )

    snap = _load_scan_snapshot(store)
    assert snap["startedAt"] == "2026-05-09T10:00:00Z"
    assert snap["finishedAt"].endswith("Z")
    assert isinstance(snap["durationMs"], int)
    # failedSources 可能为空或非空（取决于具体异常路径），只要 finishedAt 齐全即可
    assert "failedSources" in snap
