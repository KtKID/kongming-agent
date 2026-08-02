from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from infrastructure.config.models import Config, ModelSelectionConfig
from sitian.config import SiTianAnalyzerConfig, SiTianConfig, SiTianSourceConfig
from sitian.models import SiTianReport
from sitian.scanners import SiTianScanSource
from sitian.service import SiTianReadState, SiTianRunOnce
from sitian.store import SiTianRecordsStore


def _build_cfg(*, source: SiTianSourceConfig, analyzer_enabled: bool = False) -> Config:
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        sitian=SiTianConfig(
            default_scan_interval_sec=60,
            idle_sleep_sec=2,
            analyzer=SiTianAnalyzerConfig(
                enabled=analyzer_enabled,
                preset_id="local-gemma-4-e4b-it",
                skip_if_unchanged=False,
            ),
            sources=[source],
        ),
    )


@pytest.mark.unit
def test_sitian_run_once_materializes_workspace_state(tmp_path: Path) -> None:
    project_dir = tmp_path / "general-channel"
    project_dir.mkdir()
    (project_dir / "todo.md").write_text("# TODO\nfinish sitian\n", encoding="utf-8")
    (project_dir / "result.txt").write_text("done", encoding="utf-8")

    cfg = _build_cfg(
        source=SiTianSourceConfig(
            id="general",
            kind="generic_channel",
            path=str(project_dir),
            scan_interval_sec=30,
        )
    )
    store = SiTianRecordsStore(tmp_path / "records")

    result = asyncio.run(
        SiTianRunOnce(
            cfg,
            store=store,
            now=datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC),
        )
    )

    assert result.failed_sources == {}
    assert result.scanned_source_ids == ("general",)
    state_payload = asyncio.run(SiTianReadState(store=store))
    assert state_payload["workspaceState"] is not None
    workspace_state = dict(state_payload["workspaceState"])
    assert workspace_state["sources"]["total"] == 1
    assert len(workspace_state["workItems"]) == 1
    assert "SiTian Summary" in str(state_payload["latestSummary"])
    assert (store.root_dir / "workspace_state.json").exists()
    assert (store.root_dir / "latest_suggestions.json").exists()


@pytest.mark.unit
def test_sitian_run_once_writes_analyzer_markdown_to_latest_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sitian.analyzer as analyzer_mod

    project_dir = tmp_path / "general-channel"
    project_dir.mkdir()
    (project_dir / "todo.md").write_text("# TODO\nfinish sitian\n", encoding="utf-8")

    async def _fake_analyze(*args, **kwargs) -> SiTianReport:
        return SiTianReport(
            report_id="report-test",
            generated_at="2026-05-09T10:00:00Z",
            summary="analyzer summary",
            model_name="fake-model",
            errors=(),
            top_alerts=(),
            projects=(),
        )

    monkeypatch.setattr(analyzer_mod, "sitian_analyze", _fake_analyze)

    cfg = _build_cfg(
        source=SiTianSourceConfig(
            id="general",
            kind="generic_channel",
            path=str(project_dir),
            scan_interval_sec=30,
        ),
        analyzer_enabled=True,
    )
    store = SiTianRecordsStore(tmp_path / "records")

    fake_provider: object = object()
    asyncio.run(
        SiTianRunOnce(
            cfg,
            store=store,
            now=datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC),
            llm_provider=fake_provider,  # type: ignore[arg-type]
        )
    )

    summary_text = (store.root_dir / "latest_summary.md").read_text(encoding="utf-8")
    analysis_text = (store.root_dir / "latest_analysis.md").read_text(encoding="utf-8")
    state_payload = asyncio.run(SiTianReadState(store=store))

    assert summary_text.startswith("# SiTian Summary")
    assert analysis_text.startswith("# 司天巡检 2026-05-09T10:00:00Z")
    assert "analyzer summary" in analysis_text
    assert state_payload["latestSummary"] == summary_text
    assert state_payload["latestAnalysis"] == analysis_text


@pytest.mark.unit
def test_sitian_scan_claude_project_reads_matching_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "repo-alpha"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("hello", encoding="utf-8")

    claude_home = tmp_path / ".claude"
    session_dir = claude_home / "projects" / "encoded"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "thread-1.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "cwd": str(project_dir),
                        "message": {"role": "user", "content": "Investigate blocker"},
                    }
                ),
                json.dumps({"type": "assistant", "cwd": str(project_dir)}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="claude-alpha",
                kind="claude_project",
                path=str(project_dir),
            ),
            observed_at="2026-05-09T10:00:00Z",
        )
    )

    thread_observations = [item for item in batch.observations if item.entity_type == "thread"]
    assert len(thread_observations) == 1
    assert thread_observations[0].payload["title"] == "Investigate blocker"


@pytest.mark.unit
def test_sitian_scan_codex_project_reads_matching_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "repo-beta"
    project_dir.mkdir()
    (project_dir / "notes.txt").write_text("hello", encoding="utf-8")

    codex_home = tmp_path / ".codex"
    rollout_dir = codex_home / "sessions" / "2026" / "05" / "09"
    rollout_dir.mkdir(parents=True)
    session_id = "019c429a-b3e2-7f00-a1d2-e4f5a6b7c8d9"
    rollout_file = rollout_dir / f"rollout-2026-05-09T10-00-00-{session_id}.jsonl"
    rollout_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "cwd": str(project_dir),
                            "originator": "codex_exec",
                            "cli_version": "0.128.0",
                        },
                    }
                ),
                json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (codex_home / "session_index.jsonl").write_text(
        json.dumps(
            {
                "id": session_id,
                "thread_name": "Codex progress",
                "updated_at": "2026-05-09T10:10:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="codex-beta",
                kind="codex_project",
                path=str(project_dir),
            ),
            observed_at="2026-05-09T10:00:00Z",
        )
    )

    thread_observations = [item for item in batch.observations if item.entity_type == "thread"]
    assert len(thread_observations) == 1
    assert thread_observations[0].payload["title"] == "Codex progress"


@pytest.mark.unit
def test_sitian_scan_claude_workspace_lists_top_projects_by_mtime(
    tmp_path: Path,
) -> None:
    claude_home = tmp_path / ".claude"
    projects_dir = claude_home / "projects"
    projects_dir.mkdir(parents=True)

    # 造 5 个 project，jsonl mtime 故意倒着设：proj-1 最旧，proj-5 最新
    base_ts = 1_704_067_200.0  # 2024-01-01 UTC
    for i in range(1, 6):
        project_dir = projects_dir / f"-Volumes-proj-{i}"
        project_dir.mkdir()
        session_file = project_dir / f"thread-{i}.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "user",
                    "cwd": f"/Volumes/proj/{i}",
                    "message": {"role": "user", "content": f"task {i}"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(session_file, (base_ts + i * 100, base_ts + i * 100))

    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="claude-recent",
                kind="claude_workspace",
                path=str(claude_home),
                top_n=3,
            ),
            observed_at="2026-05-10T10:00:00Z",
        )
    )

    project_obs = [o for o in batch.observations if o.entity_type == "project"]
    assert len(project_obs) == 3
    assert [o.payload["rank"] for o in project_obs] == [1, 2, 3]
    assert [o.payload["displayName"] for o in project_obs] == ["5", "4", "3"]

    status_obs = [o for o in batch.observations if o.entity_type == "status"]
    assert len(status_obs) == 1
    assert status_obs[0].payload["projectCount"] == 3
    assert status_obs[0].payload["topN"] == 3

    thread_obs = [o for o in batch.observations if o.entity_type == "thread"]
    assert len(thread_obs) == 3
    assert thread_obs[0].payload["title"] == "task 5"


@pytest.mark.unit
def test_sitian_scan_claude_workspace_accepts_projects_subdir_path(
    tmp_path: Path,
) -> None:
    claude_home = tmp_path / ".claude"
    projects_dir = claude_home / "projects"
    projects_dir.mkdir(parents=True)
    project_dir = projects_dir / "-Volumes-only"
    project_dir.mkdir()
    (project_dir / "t.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "cwd": "/Volumes/only",
                "message": {"role": "user", "content": "hello"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # path 传 projects 子目录也应识别到 claude_home
    batch = asyncio.run(
        SiTianScanSource(
            SiTianSourceConfig(
                id="claude-recent",
                kind="claude_workspace",
                path=str(projects_dir),
            ),
            observed_at="2026-05-10T10:00:00Z",
        )
    )
    project_obs = [o for o in batch.observations if o.entity_type == "project"]
    assert len(project_obs) == 1
    assert project_obs[0].payload["cwd"] == "/Volumes/only"


@pytest.mark.unit
def test_sitian_suggestions_claude_workspace_produces_per_project_work_items(
    tmp_path: Path,
) -> None:
    """claude_workspace 扫到 3 个项目 → suggestions 产出 3 个独立 work_items。"""

    claude_home = tmp_path / ".claude"
    projects_dir = claude_home / "projects"
    projects_dir.mkdir(parents=True)

    base_ts = 1_704_067_200.0
    for i in range(1, 4):
        project_dir = projects_dir / f"-Volumes-proj-{i}"
        project_dir.mkdir()
        session_file = project_dir / f"thread-{i}.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "user",
                    "cwd": f"/Volumes/proj/{i}",
                    "message": {"role": "user", "content": f"task {i}"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(session_file, (base_ts + i * 100, base_ts + i * 100))

    cfg = Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        sitian=SiTianConfig(
            default_scan_interval_sec=60,
            idle_sleep_sec=2,
            sources=[
                SiTianSourceConfig(
                    id="claude-recent",
                    kind="claude_workspace",
                    path=str(claude_home),
                    top_n=3,
                ),
            ],
        ),
    )
    store = SiTianRecordsStore(tmp_path / "records")
    result = asyncio.run(
        SiTianRunOnce(cfg, store=store, now=datetime(2026, 5, 10, 10, 0, 0, tzinfo=UTC))
    )
    assert result.observation_count == 7  # 1 status + 3 project + 3 thread

    state_payload = asyncio.run(SiTianReadState(store=store))
    ws = state_payload["workspaceState"]
    assert ws is not None

    work_items = ws["workItems"]
    # 关键断言：3 个项目 → 3 个 work_items（不是 1 个）
    assert len(work_items) == 3

    # title 是 displayName（项目目录名的最后一段），不是 thread 消息
    titles = [item["title"] for item in work_items]
    assert "3" in titles
    assert "2" in titles
    assert "1" in titles

    # 每个 work_item 都不是 blocked（不再误判"目录中没有可观察文件"）
    for item in work_items:
        assert item["status"] != "blocked", f"{item['title']} should not be blocked"
        blockers = item.get("blockers", [])
        assert "目录中没有可观察文件" not in blockers

    # summary 应列出 3 个 work items
    summary = state_payload["latestSummary"]
    assert "Work items: 3" in summary
