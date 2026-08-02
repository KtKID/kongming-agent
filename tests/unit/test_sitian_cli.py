from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

import sitian.cli as sitian_cli
from infrastructure.config.models import Config, ModelSelectionConfig
from sitian.config import SiTianConfig, SiTianSourceConfig


def _build_cfg(project_dir: Path, *, output_subdir: str | None = None) -> Config:
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
        sitian=SiTianConfig(
            default_scan_interval_sec=60,
            idle_sleep_sec=2,
            output_subdir=output_subdir,
            sources=[
                SiTianSourceConfig(
                    id="general",
                    kind="generic_channel",
                    path=str(project_dir),
                )
            ],
        ),
    )


def test_sitian_cli_run_once_and_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "channel"
    project_dir.mkdir()
    (project_dir / "a.md").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(sitian_cli, "load_config", lambda _path=None: _build_cfg(project_dir))
    runner = CliRunner()

    run_once = runner.invoke(
        sitian_cli.main,
        ["run-once", "--root-dir", str(tmp_path / "records")],
    )
    assert run_once.exit_code == 0
    run_payload = json.loads(run_once.output)
    assert run_payload["scannedSourceIds"] == ["general"]

    state = runner.invoke(
        sitian_cli.main,
        ["state", "--root-dir", str(tmp_path / "records")],
    )
    assert state.exit_code == 0
    state_payload = json.loads(state.output)
    assert state_payload["workspaceState"]["sources"]["total"] == 1


def test_sitian_cli_output_subdir_routes_to_subdir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """output_subdir=claude 时所有产物落到 <root>/claude/。"""
    project_dir = tmp_path / "channel"
    project_dir.mkdir()
    (project_dir / "a.md").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(
        sitian_cli,
        "load_config",
        lambda _path=None: _build_cfg(project_dir, output_subdir="claude"),
    )
    runner = CliRunner()
    root_dir = tmp_path / "records"

    run_once = runner.invoke(
        sitian_cli.main,
        ["run-once", "--root-dir", str(root_dir)],
    )
    assert run_once.exit_code == 0

    # 关键断言：产物在 <root>/claude/，不是 <root>/
    assert (root_dir / "claude" / "observations.jsonl").exists()
    assert (root_dir / "claude" / "workspace_state.json").exists()
    assert (root_dir / "claude" / "latest_summary.md").exists()
    # <root>/ 下应只有子目录 claude/，不该有 observations.jsonl 直接落根
    assert not (root_dir / "observations.jsonl").exists()

    # state 命令也读子目录
    state = runner.invoke(
        sitian_cli.main,
        ["state", "--root-dir", str(root_dir)],
    )
    assert state.exit_code == 0
    state_payload = json.loads(state.output)
    assert state_payload["workspaceState"]["sources"]["total"] == 1
    # rootDir 字段应包含 claude 子目录
    assert Path(state_payload["rootDir"]).name == "claude"


def test_sitian_cli_no_output_subdir_keeps_root_layout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """不设 output_subdir 时行为跟改动前一致（向后兼容）。"""
    project_dir = tmp_path / "channel"
    project_dir.mkdir()
    (project_dir / "a.md").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(sitian_cli, "load_config", lambda _path=None: _build_cfg(project_dir))
    runner = CliRunner()
    root_dir = tmp_path / "records"

    run_once = runner.invoke(
        sitian_cli.main,
        ["run-once", "--root-dir", str(root_dir)],
    )
    assert run_once.exit_code == 0

    # 产物直接落 root_dir，不在子目录
    assert (root_dir / "observations.jsonl").exists()
    assert (root_dir / "workspace_state.json").exists()
    assert not (root_dir / "claude").exists()
