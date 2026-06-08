"""端到端 smoke 测试（v0.1.5 web-app-shell）。

最小路径：

1. 启动 app（FakeThreadManager）
2. login → 200 + cookie
3. me → 200 user_id=default
4. POST /api/threads → 201
5. GET /api/threads → 1 项
6. logout → 200
7. me → 401
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import _seed_password
from tests.unit.test_web_routers_threads import FakeTM
from web.app import create_app
from web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE

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
                "llm_presets": [
                    {
                        "id": "p1",
                        "display_name": "Preset 1",
                        "base_url": "http://127.0.0.1:1234/v1",
                        "model": "qwen-7b",
                        "api_key_env": None,
                    }
                ],
            },
        }
    )


def test_e2e_full_flow(tmp_path: Path) -> None:
    """跑一遍完整闭环。"""
    _seed_password(tmp_path, "test-pwd")
    cfg = _make_cfg()
    tm = FakeTM()
    app = create_app(cfg, tm, home_dir=tmp_path)

    with TestClient(app) as client:
        # 1. login
        r = client.post(
            "/api/auth/login",
            json={"password": "test-pwd"},
            headers=CSRF_HEADERS,
        )
        assert r.status_code == 200, r.text

        # 2. me
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["authenticated"] is True
        assert r.json()["user_id"] == "default"

        # 3. presets list
        r = client.get("/api/presets")
        assert r.status_code == 200
        presets = r.json()
        assert len(presets) == 1
        assert presets[0]["id"] == "p1"
        assert "api_key" not in presets[0]

        # 4. create thread
        r = client.post(
            "/api/threads",
            json={"name": "smoke", "preset_id": "p1"},
            headers=CSRF_HEADERS,
        )
        assert r.status_code == 201, r.text
        thread = r.json()
        thread_id = thread["id"]

        # 5. list threads
        r = client.get("/api/threads")
        assert r.status_code == 200
        body = r.json()
        assert any(t["id"] == thread_id for t in body)

        # 6. rename
        r = client.patch(
            f"/api/threads/{thread_id}",
            json={"name": "renamed"},
            headers=CSRF_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["name"] == "renamed"

        # 7. delete
        r = client.delete(
            f"/api/threads/{thread_id}",
            headers=CSRF_HEADERS,
        )
        assert r.status_code == 204

        # 8. logout
        r = client.post("/api/auth/logout", headers=CSRF_HEADERS)
        assert r.status_code == 200

        # 9. me 应失败（cookie 已清）
        r = client.get("/api/auth/me")
        assert r.status_code == 401
