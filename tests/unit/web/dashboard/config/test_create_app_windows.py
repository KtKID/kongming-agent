from __future__ import annotations

from pathlib import Path

import pytest

from hosts.web import app as web_app
from hosts.web.app import create_app
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password


class _FakeTM:
    _started = False
    _closed = False

    async def start(self) -> None:
        self._started = True

    async def aclose_all(self) -> None:
        self._closed = True

    def list_threads(self) -> list:
        return []

    def list_cells(self) -> list:
        return []

    def get_cell(self, thread_id: str) -> None:
        return None


def _make_cfg() -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {"enabled": True, "dev_mode": True},
        }
    )


def test_create_app_registers_dashboard_config_routes(tmp_path: Path) -> None:
    _seed_password(tmp_path, "pwd")

    app = create_app(_make_cfg(), _FakeTM(), home_dir=tmp_path)  # type: ignore[arg-type]

    paths = {route.path for route in app.routes}
    assert "/api/manage/config/schema" in paths
    assert "/api/manage/config/raw" in paths
    assert "/api/manage/config/save" in paths


def test_create_app_config_manager_uses_home_setting_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_password(tmp_path, "pwd")
    monkeypatch.delenv("KONGMING_CONFIG", raising=False)
    home_config = tmp_path / "setting.yaml"
    home_config.write_text(
        "model:\n  name: fake\n  base_url: http://127.0.0.1:1234/v1\n",
        encoding="utf-8",
    )

    app = create_app(_make_cfg(), _FakeTM(), home_dir=tmp_path)  # type: ignore[arg-type]

    assert app.state.config_manager._yaml_path == home_config
    assert app.state.config_manager._env_path == tmp_path / ".env"


def test_create_app_config_manager_falls_back_to_repo_setting_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_password(tmp_path, "pwd")
    monkeypatch.delenv("KONGMING_CONFIG", raising=False)

    app = create_app(_make_cfg(), _FakeTM(), home_dir=tmp_path)  # type: ignore[arg-type]

    assert app.state.config_manager._yaml_path == web_app._REPO_ROOT / "config" / "setting.yaml"
    assert app.state.config_manager._env_path == tmp_path / ".env"
