from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.test_web_routers_threads import FakeTM

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


def _make_cfg() -> Config:
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
