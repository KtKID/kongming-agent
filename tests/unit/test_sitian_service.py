from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from config_loader.models import Config, ModelConfig
from core.sitian import SiTianConfig, SiTianSourceConfig
from sitian.scanners import SiTianScanSource
from sitian.service import SiTianReadState, SiTianRunOnce
from sitian.store import SiTianRecordsStore


def _build_cfg(*, source: SiTianSourceConfig) -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="stub-model",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        sitian=SiTianConfig(
            default_scan_interval_sec=60,
            idle_sleep_sec=2,
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
