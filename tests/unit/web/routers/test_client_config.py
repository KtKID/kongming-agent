from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from infrastructure.config import load_config
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.test_web_routers_threads import FakeTM

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


def _make_cfg(*, host_environment: str = "browser") -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "web": {
                "enabled": True,
                "dev_mode": True,
                "host_environment": host_environment,
                "dashboard_poll_interval_seconds": 1,
            },
            "scheduler": {"default_timezone": "Asia/Shanghai"},
        }
    )


def test_client_config_returns_configured_timezone(tmp_path: Path) -> None:
    _seed_password(tmp_path, "test-pwd")
    app = create_app(_make_cfg(), FakeTM(), home_dir=tmp_path)

    with TestClient(app) as client:
        login_response = client.post(
            "/api/auth/login",
            json={"password": "test-pwd"},
            headers=CSRF_HEADERS,
        )
        assert login_response.status_code == 200
        response = client.get("/api/config/client")

    assert response.status_code == 200
    payload = response.json()
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["dashboard_poll_interval_seconds"] == 3
    assert payload["ws_heartbeat_interval_ms"] == 30000
    assert payload["host_environment"] == "browser"
    assert payload["capabilities"] == {
        "xspace_host": False,
        "native_file_dialog": False,
    }


def test_client_config_returns_xspace_capabilities(tmp_path: Path) -> None:
    _seed_password(tmp_path, "test-pwd")
    app = create_app(_make_cfg(host_environment="xspace"), FakeTM(), home_dir=tmp_path)

    with TestClient(app) as client:
        login_response = client.post(
            "/api/auth/login",
            json={"password": "test-pwd"},
            headers=CSRF_HEADERS,
        )
        assert login_response.status_code == 200
        response = client.get("/api/config/client")

    assert response.status_code == 200
    payload = response.json()
    assert payload["host_environment"] == "xspace"
    assert payload["capabilities"] == {
        "xspace_host": True,
        "native_file_dialog": True,
    }


def test_client_config_smoke_migrates_legacy_config_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Web 旧配置启动 smoke：迁移真实 YAML 后返回客户端心跳配置。"""
    config_path = tmp_path / "setting.yaml"
    config_path.write_text(
        """\
model:
  name: fake
  base_url: http://127.0.0.1:1234/v1
  api_key: ""
web:
  enabled: true
  dev_mode: true
scheduler:
  default_timezone: Asia/Shanghai
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("KONGMING_CONFIG", str(config_path))
    _seed_password(tmp_path, "test-pwd")
    cfg = load_config(config_path, load_env_file=False)
    app = create_app(cfg, FakeTM(), home_dir=tmp_path)

    with TestClient(app) as client:
        login_response = client.post(
            "/api/auth/login",
            json={"password": "test-pwd"},
            headers=CSRF_HEADERS,
        )
        assert login_response.status_code == 200
        response = client.get("/api/config/client")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ws_heartbeat_interval_ms"] == 30000
    assert payload["ws_heartbeat_background_interval_ms"] == 60000
    assert payload["timezone"] == "Asia/Shanghai"
    migrated_text = config_path.read_text(encoding="utf-8")
    assert migrated_text.splitlines()[0].startswith("config_schema_version: v0.5")
    assert "配置结构版本；当前版本为 v0.5。" in migrated_text
    assert "api.moonshot.cn" not in migrated_text
