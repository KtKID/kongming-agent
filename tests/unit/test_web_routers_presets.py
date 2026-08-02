"""catalog v2 preset Web 路由合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.routers.presets import _redact_base_url
from infrastructure.config.models import Config
from tests.unit.test_web_app_lifespan import FakeThreadManager, _seed_password

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


@pytest.fixture(autouse=True)
def _isolate_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清理真实 provider credential，保持列表可预测。"""
    for name in (
        "MINIMAX_API_KEY",
        "KONGMING_PROVIDER_MINIMAX_API_KEY",
        "GLM_API_KEY",
        "BIGMODEL_API_KEY",
        "ZHIPU_API_KEY",
        "ZAI_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _make_cfg() -> Config:
    """构造仅保存运行选择的 v0.6 Web 配置。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "local-gemma-4-e4b-it"},
            "web": {"enabled": True, "dev_mode": True},
        }
    )


def _login_client(tmp_path: Path, cfg: Config) -> TestClient:
    """创建带真实鉴权中间件和路由的 client。"""
    _seed_password(tmp_path, "pwd")
    app = create_app(cfg, FakeThreadManager(), home_dir=tmp_path)
    client = TestClient(app)
    client.__enter__()
    response = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200
    return client


def test_redact_base_url_remote_keeps_host_only() -> None:
    assert _redact_base_url("https://api.openai.com/v1") == "api.openai.com"
    assert _redact_base_url("https://api.example.com:8443/v2") == "api.example.com:8443"


def test_redact_base_url_local_kept_intact() -> None:
    assert _redact_base_url("http://127.0.0.1:1234/v1") == "http://127.0.0.1:1234/v1"


def test_list_presets_returns_connected_catalog_models_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """credential 可用的远端模型与本地模型进入脱敏列表。"""
    monkeypatch.setenv("GLM_API_KEY", "glm-live")
    cfg = _make_cfg()
    client = _login_client(tmp_path, cfg)
    try:
        response = client.get("/api/presets")
        assert response.status_code == 200
        body = {item["id"]: item for item in response.json()}
        assert set(body) == {
            "bigmodel-glm5-1m",
            "bigmodel-glm5",
            "local-gemma-4-e4b-it",
        }
        assert body["bigmodel-glm5-1m"] == {
            "id": "bigmodel-glm5-1m",
            "display_name": "glm-5.2",
            "model": "glm-5.2",
            "base_url_summary": "open.bigmodel.cn",
            "requires_api_key": True,
        }
        assert body["local-gemma-4-e4b-it"]["base_url_summary"] == ("http://127.0.0.1:62000/v1")
        assert "api_key" not in body["bigmodel-glm5-1m"]
        assert set(cfg.model.model_dump()) == {"preset_id", "reasoning_effort"}
    finally:
        client.__exit__(None, None, None)


def test_list_presets_with_no_remote_credentials_keeps_local_model(tmp_path: Path) -> None:
    """远端 credential 缺失时保留可直接运行的本地模型。"""
    client = _login_client(tmp_path, _make_cfg())
    try:
        response = client.get("/api/presets")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == ["local-gemma-4-e4b-it"]
    finally:
        client.__exit__(None, None, None)


def test_presets_unauthenticated(tmp_path: Path) -> None:
    cfg = _make_cfg()
    _seed_password(tmp_path, "pwd")
    app = create_app(cfg, FakeThreadManager(), home_dir=tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/presets")
        assert response.status_code == 401
