from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

import sitian.cli as sitian_cli
from config_loader.models import Config, ModelConfig
from sitian.config import SiTianConfig, SiTianSourceConfig


def _build_cfg(project_dir: Path) -> Config:
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
