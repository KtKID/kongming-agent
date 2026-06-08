"""routers/presets.py 单测（v0.1.5）。

覆盖：

1. 多 preset 全字段返回
2. api_key 不会出现在响应里
3. base_url 远端只保留 host[:port]；本地原样
4. requires_api_key 反映 api_key_env 是否非 None
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import FakeThreadManager, _seed_password
from web.app import create_app
from web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from web.routers.presets import _redact_base_url

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


def _make_cfg(presets: list[dict[str, Any]]) -> Config:
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
                "llm_presets": presets,
            },
        }
    )


def test_redact_base_url_remote_keeps_host_only() -> None:
    assert _redact_base_url("https://api.openai.com/v1") == "api.openai.com"
    assert _redact_base_url("https://api.anthropic.com/some/path") == "api.anthropic.com"


def test_redact_base_url_remote_preserves_port() -> None:
    assert _redact_base_url("https://api.example.com:8443/v2") == "api.example.com:8443"


def test_redact_base_url_local_kept_intact() -> None:
    assert _redact_base_url("http://127.0.0.1:1234/v1") == "http://127.0.0.1:1234/v1"
    assert _redact_base_url("http://localhost:5000/api") == "http://localhost:5000/api"


def _login_client(tmp_path: Path, cfg: Config) -> TestClient:
    _seed_password(tmp_path, "pwd")
    tm = FakeThreadManager()
    app = create_app(cfg, tm, home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    resp = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200
    return client


def test_list_presets_full_fields(tmp_path: Path) -> None:
    presets = [
        {
            "id": "openai-gpt-5",
            "display_name": "OpenAI GPT-5",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5",
            "api_key_env": "OPENAI_API_KEY",
        },
        {
            "id": "local-qwen",
            "display_name": "Local Qwen",
            "base_url": "http://127.0.0.1:1234/v1",
            "model": "qwen-7b",
            "api_key_env": None,
        },
    ]
    cfg = _make_cfg(presets)
    client = _login_client(tmp_path, cfg)
    try:
        resp = client.get("/api/presets")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2

        openai = next(p for p in body if p["id"] == "openai-gpt-5")
        assert openai["display_name"] == "OpenAI GPT-5"
        assert openai["model"] == "gpt-5"
        assert openai["base_url_summary"] == "api.openai.com"
        assert openai["requires_api_key"] is True
        # 关键：不返回 api_key / api_key_env
        assert "api_key" not in openai
        assert "api_key_env" not in openai

        local = next(p for p in body if p["id"] == "local-qwen")
        assert local["base_url_summary"] == "http://127.0.0.1:1234/v1"
        assert local["requires_api_key"] is False
    finally:
        client.__exit__(None, None, None)


def test_empty_presets(tmp_path: Path) -> None:
    cfg = _make_cfg([])
    client = _login_client(tmp_path, cfg)
    try:
        resp = client.get("/api/presets")
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        client.__exit__(None, None, None)


def test_presets_unauthenticated(tmp_path: Path) -> None:
    cfg = _make_cfg([])
    _seed_password(tmp_path, "pwd")
    tm = FakeThreadManager()
    app = create_app(cfg, tm, home_dir=tmp_path)
    with TestClient(app) as client:
        resp = client.get("/api/presets")
        assert resp.status_code == 401
