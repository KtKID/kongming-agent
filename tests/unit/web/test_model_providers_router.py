"""模型 provider Web 路由的 catalog v2 合同测试。"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.routers import model_providers as router_mod
from hosts.web.routers.model_providers import router
from infrastructure.config.model_catalog_manager import ModelCatalogManager


@pytest.fixture(autouse=True)
def _isolate_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清理 credential 环境变量，保证连接状态由单测显式控制。"""
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


class _FakeConfigManager:
    """记录 env 写入并同步进程环境。"""

    def __init__(self) -> None:
        self.writes: list[dict[str, str]] = []

    def write_env_values(self, values: dict[str, str]) -> None:
        """记录并应用 provider credential。"""
        self.writes.append(values)
        os.environ.update(values)


def _client(*, config_manager: _FakeConfigManager | None = None) -> TestClient:
    """创建注入真实 ModelCatalogManager 的最小 FastAPI client。"""
    app = FastAPI()
    app.state.model_catalog_manager = ModelCatalogManager()
    if config_manager is not None:
        app.state.config_manager = config_manager
    app.include_router(router)
    return TestClient(app)


def test_catalog_projects_builtin_provider_dto() -> None:
    """catalog endpoint 直接投影 v2 provider 静态字段。"""
    response = _client().get("/api/model-providers/catalog")

    assert response.status_code == 200
    providers = {item["providerId"]: item for item in response.json()}
    assert providers["glm"] == {
        "providerId": "glm",
        "displayName": "GLM",
        "regionLabel": "CN",
        "description": "智谱 GLM API Key，用于启用 GLM 模型预设。",
        "logoText": "G",
    }
    assert "local-baseline" in providers


def test_connections_are_derived_from_catalog_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """远端连接状态来自 provider-specific env，本地 endpoint 自动可用。"""
    monkeypatch.setenv("GLM_API_KEY", "glm-live")

    response = _client().get("/api/model-providers/connections")

    assert response.status_code == 200
    connections = {item["providerId"]: item for item in response.json()}
    assert connections["glm"] == {
        "providerId": "glm",
        "status": "connected",
        "model": "glm-5.2",
        "authLabel": "Bearer",
    }
    assert connections["minimax"]["status"] == "disconnected"
    assert connections["local-baseline"]["status"] == "connected"


def test_connect_writes_catalog_credential_reference() -> None:
    """connect 只写 catalog 声明的 credential env。"""
    config_manager = _FakeConfigManager()

    response = _client(config_manager=config_manager).post(
        "/api/model-providers/glm/connect",
        json={"apiKey": "glm-live"},
    )

    assert response.status_code == 200
    assert config_manager.writes == [{"GLM_API_KEY": "glm-live"}]
    assert response.json()["connection"] == {
        "providerId": "glm",
        "status": "connected",
        "model": "glm-5.2",
        "authLabel": "Bearer",
    }


def test_model_families_project_reasoning_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composer 能力、默认 effort 与 context 来自同一 catalog model。"""
    monkeypatch.setenv("GLM_API_KEY", "glm-live")

    response = _client().get("/api/model-providers/model-families")

    assert response.status_code == 200
    families = {item["presetId"]: item for item in response.json()}
    assert families["bigmodel-glm5-1m"]["supportedReasoningEfforts"] == [
        "none",
        "low",
        "medium",
        "high",
    ]
    assert families["bigmodel-glm5-1m"]["defaultReasoningEffort"] == "high"
    assert families["bigmodel-glm5-1m"]["reasoningAdapter"] == "glm_thinking_toggle"
    assert families["bigmodel-glm5-1m"]["contextWindowTokens"] == 1_000_000
    assert families["local-gemma-4-e4b-it"]["supportedReasoningEfforts"] == []


def test_probe_route_uses_catalog_default_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """test endpoint 用 provider 默认 model 发 probe，并返回共享 DTO。"""
    seen: dict[str, str] = {}

    async def fake_probe(*, definition, model, api_key):
        """捕获路由解析出的 provider、model 和临时 credential。"""
        seen.update(
            provider_id=definition.provider_id,
            model=model.model,
            api_key=api_key,
        )
        return router_mod._ProbeResult(ok=True, message="连接测试通过。")

    monkeypatch.setattr(router_mod, "_probe_provider", fake_probe)

    response = _client().post(
        "/api/model-providers/glm/test",
        json={"apiKey": "temporary-key"},
    )

    assert response.status_code == 200
    assert seen == {
        "provider_id": "glm",
        "model": "glm-5.2",
        "api_key": "temporary-key",
    }
    assert response.json() == {
        "providerId": "glm",
        "ok": True,
        "message": "连接测试通过。",
        "connection": None,
    }


def test_unknown_provider_has_stable_public_shape() -> None:
    """未知 provider 返回确定的业务响应。"""
    response = _client().post(
        "/api/model-providers/missing/connect",
        json={"apiKey": "any-key"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "providerId": "missing",
        "ok": False,
        "message": "未知模型服务商。",
        "connection": None,
    }
