from __future__ import annotations

import os
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hosts.web.routers import model_providers as router_mod
from hosts.web.routers.model_providers import router
from infrastructure.config.models import LLMPresetConfig


@pytest.fixture(autouse=True)
def _isolate_provider_env(monkeypatch) -> None:
    """清理模型服务商相关环境变量，避免开发机真实 key 污染单测。"""
    for name in (
        "MINIMAX_API_KEY",
        "KONGMING_PROVIDER_MINIMAX_API_KEY",
        "KONGMING_MODEL_API_KEY",
        "KONGMING_MODEL_BASE_URL",
        "KONGMING_MODEL_NAME",
        "GLM_API_KEY",
        "BIGMODEL_API_KEY",
        "ZHIPU_API_KEY",
        "ZAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "CUSTOM_MINIMAX_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _config(presets: list[LLMPresetConfig] | None = None) -> SimpleNamespace:
    return SimpleNamespace(web=SimpleNamespace(llm_presets=presets or []))


def _client(cfg: SimpleNamespace) -> TestClient:
    app = FastAPI()
    app.state.config = cfg
    app.include_router(router)
    return TestClient(app)


class _FakeConfigManager:
    """测试 config manager：记录写入并同步当前进程 env。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.writes: list[dict[str, str]] = []
        self.presets: list[LLMPresetConfig] = []

    def write_env_values(self, values: dict[str, str]) -> None:
        self.writes.append(values)
        for key, value in values.items():
            self._monkeypatch.setenv(key, value)

    def upsert_web_llm_preset(self, preset: LLMPresetConfig) -> None:
        self.presets.append(preset)


def _client_with_config_manager(
    cfg: SimpleNamespace,
    config_manager: _FakeConfigManager,
) -> TestClient:
    app = FastAPI()
    app.state.config = cfg
    app.state.config_manager = config_manager
    app.include_router(router)
    return TestClient(app)


def test_catalog_returns_supported_providers() -> None:
    client = _client(_config())

    resp = client.get("/api/model-providers/catalog")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "providerId": "minimax",
            "displayName": "Minimax",
            "regionLabel": "CN",
            "description": "中国区 Minimax API Key，用于启用对应模型预设。",
            "logoText": "M",
        },
        {
            "providerId": "glm",
            "displayName": "GLM",
            "regionLabel": "CN",
            "description": "智谱 GLM API Key，用于启用 GLM 模型预设。",
            "logoText": "G",
        },
        {
            "providerId": "deepseek",
            "displayName": "DeepSeek",
            "regionLabel": "CN",
            "description": "DeepSeek API Key，用于启用 DeepSeek 模型预设。",
            "logoText": "D",
        },
    ]


def test_connections_reads_stable_minimax_env(monkeypatch) -> None:
    monkeypatch.setenv("KONGMING_PROVIDER_MINIMAX_API_KEY", "sk-live")
    client = _client(_config())

    resp = client.get("/api/model-providers/connections")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "providerId": "minimax",
            "status": "connected",
            "model": None,
            "authLabel": "Bearer",
        },
        {
            "providerId": "glm",
            "status": "disconnected",
            "model": None,
            "authLabel": None,
        },
        {
            "providerId": "deepseek",
            "status": "disconnected",
            "model": None,
            "authLabel": None,
        },
    ]


def test_connections_reads_minimax_preset_env(monkeypatch) -> None:
    monkeypatch.setenv("CUSTOM_MINIMAX_KEY", "sk-live")
    preset = LLMPresetConfig(
        id="minimax-cn",
        display_name="Minimax（CN）",
        provider="anthropic",
        base_url="https://api.minimaxi.com/anthropic",
        model="MiniMax-M3",
        api_key_env="CUSTOM_MINIMAX_KEY",
    )
    client = _client(_config([preset]))

    resp = client.get("/api/model-providers/connections")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "providerId": "minimax",
            "status": "connected",
            "model": "MiniMax-M3",
            "authLabel": "Bearer",
        },
        {
            "providerId": "glm",
            "status": "disconnected",
            "model": None,
            "authLabel": None,
        },
        {
            "providerId": "deepseek",
            "status": "disconnected",
            "model": None,
            "authLabel": None,
        },
    ]


def test_connect_provider_writes_default_env_and_returns_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeConfigManager(monkeypatch)
    preset = LLMPresetConfig(
        id="minimax-cn",
        display_name="Minimax（CN）",
        provider="anthropic",
        base_url="https://api.minimaxi.com/anthropic",
        model="MiniMax-M3",
        api_key_env="CUSTOM_MINIMAX_KEY",
    )
    client = _client_with_config_manager(_config([preset]), manager)

    resp = client.post(
        "/api/model-providers/minimax/connect",
        json={"apiKey": "sk-live"},
    )

    assert resp.status_code == 200
    assert manager.writes == [{"MINIMAX_API_KEY": "sk-live"}]
    assert [item.api_key_env for item in manager.presets] == ["MINIMAX_API_KEY"]
    assert resp.json() == {
        "providerId": "minimax",
        "ok": True,
        "message": "已保存，刚刚测试通过。",
        "connection": {
            "providerId": "minimax",
            "status": "connected",
            "model": "MiniMax-M3",
            "authLabel": "Bearer",
        },
    }


def test_connect_provider_without_preset_creates_selectable_model_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeConfigManager(monkeypatch)
    cfg = _config()
    client = _client_with_config_manager(cfg, manager)

    resp = client.post(
        "/api/model-providers/minimax/connect",
        json={"apiKey": "sk-live"},
    )

    assert resp.status_code == 200
    assert manager.writes == [{"MINIMAX_API_KEY": "sk-live"}]
    assert [preset.id for preset in manager.presets] == ["minimax-m3"]

    families_resp = client.get("/api/model-providers/model-families")

    assert families_resp.status_code == 200
    assert families_resp.json() == [
        {
            "providerId": "minimax",
            "providerLabel": "Minimax（CN）",
            "familyId": "minimax:MiniMax-M3",
            "displayName": "MiniMax-M3",
            "presetId": "minimax-m3",
            "model": "MiniMax-M3",
            "connected": True,
        }
    ]


def test_connections_without_env_is_disconnected(monkeypatch) -> None:
    monkeypatch.delenv("KONGMING_PROVIDER_MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("KONGMING_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client = _client(_config())

    resp = client.get("/api/model-providers/connections")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "providerId": "minimax",
            "status": "disconnected",
            "model": None,
            "authLabel": None,
        },
        {
            "providerId": "glm",
            "status": "disconnected",
            "model": None,
            "authLabel": None,
        },
        {
            "providerId": "deepseek",
            "status": "disconnected",
            "model": None,
            "authLabel": None,
        },
    ]


def test_connections_reads_glm_default_env(monkeypatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "glm-live")
    preset = LLMPresetConfig(
        id="bigmodel-glm5",
        display_name="智谱 GLM-5.1",
        provider="openai_compatible",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        model="glm-5.1",
        api_key_env="GLM_API_KEY",
    )
    client = _client(_config([preset]))

    resp = client.get("/api/model-providers/connections")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "providerId": "minimax",
            "status": "disconnected",
            "model": None,
            "authLabel": None,
        },
        {
            "providerId": "glm",
            "status": "connected",
            "model": "glm-5.1",
            "authLabel": "Bearer",
        },
        {
            "providerId": "deepseek",
            "status": "disconnected",
            "model": None,
            "authLabel": None,
        },
    ]


def test_connections_ignore_generic_model_key_only(monkeypatch) -> None:
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "minimax-live")
    monkeypatch.setenv("KONGMING_MODEL_BASE_URL", "https://api.minimaxi.com/anthropic")
    monkeypatch.setenv("KONGMING_MODEL_NAME", "MiniMax-M3")
    client = _client(_config())

    resp = client.get("/api/model-providers/connections")

    assert resp.status_code == 200
    assert [item["status"] for item in resp.json()] == [
        "disconnected",
        "disconnected",
        "disconnected",
    ]


def test_connections_old_glm_preset_generic_model_key_stays_disconnected(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "glm-live")
    preset = LLMPresetConfig(
        id="bigmodel-glm5",
        display_name="智谱 GLM-5.1",
        provider="openai_compatible",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        model="glm-5.1",
        api_key_env="KONGMING_MODEL_API_KEY",
    )
    client = _client(_config([preset]))

    resp = client.get("/api/model-providers/connections")

    assert resp.status_code == 200
    glm = next(item for item in resp.json() if item["providerId"] == "glm")
    assert glm == {
        "providerId": "glm",
        "status": "disconnected",
        "model": "glm-5.1",
        "authLabel": None,
    }


def test_connections_reads_provider_specific_glm_env_before_generic_model_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "minimax-live")
    monkeypatch.setenv("GLM_API_KEY", "glm-live")
    preset = LLMPresetConfig(
        id="bigmodel-glm5",
        display_name="智谱 GLM-5.1",
        provider="openai_compatible",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        model="glm-5.1",
        api_key_env="KONGMING_MODEL_API_KEY",
    )
    client = _client(_config([preset]))

    resp = client.get("/api/model-providers/connections")

    assert resp.status_code == 200
    glm = next(item for item in resp.json() if item["providerId"] == "glm")
    assert glm == {
        "providerId": "glm",
        "status": "connected",
        "model": "glm-5.1",
        "authLabel": "Bearer",
    }


def test_connections_reads_glm_fallback_env(monkeypatch) -> None:
    monkeypatch.setenv("BIGMODEL_API_KEY", "glm-fallback")
    client = _client(_config())

    resp = client.get("/api/model-providers/connections")

    assert resp.status_code == 200
    glm = next(item for item in resp.json() if item["providerId"] == "glm")
    assert glm == {
        "providerId": "glm",
        "status": "connected",
        "model": None,
        "authLabel": "Bearer",
    }


def test_connect_glm_writes_default_env_for_old_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeConfigManager(monkeypatch)
    preset = LLMPresetConfig(
        id="bigmodel-glm5",
        display_name="智谱 GLM-5.1",
        provider="openai_compatible",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        model="glm-5.1",
        api_key_env="KONGMING_MODEL_API_KEY",
    )
    client = _client_with_config_manager(_config([preset]), manager)

    resp = client.post("/api/model-providers/glm/connect", json={"apiKey": "glm-live"})

    assert resp.status_code == 200
    assert manager.writes == [{"GLM_API_KEY": "glm-live"}]
    assert [item.api_key_env for item in manager.presets] == ["GLM_API_KEY"]
    assert resp.json()["connection"] == {
        "providerId": "glm",
        "status": "connected",
        "model": "glm-5.1",
        "authLabel": "Bearer",
    }


def test_connections_reads_deepseek_preset_env(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-live")
    preset = LLMPresetConfig(
        id="deepseek",
        display_name="deepseek-v4-flash",
        provider="anthropic",
        base_url="https://api.deepseek.com/anthropic",
        model="deepseek-v4-flash",
        api_key_env="DEEPSEEK_API_KEY",
    )
    client = _client(_config([preset]))

    resp = client.get("/api/model-providers/connections")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "providerId": "minimax",
            "status": "disconnected",
            "model": None,
            "authLabel": None,
        },
        {
            "providerId": "glm",
            "status": "disconnected",
            "model": None,
            "authLabel": None,
        },
        {
            "providerId": "deepseek",
            "status": "connected",
            "model": "deepseek-v4-flash",
            "authLabel": "Bearer",
        },
    ]


def test_model_families_returns_only_connected_real_presets(monkeypatch) -> None:
    monkeypatch.setenv("CUSTOM_MINIMAX_KEY", "minimax-live")
    monkeypatch.setenv("GLM_API_KEY", "glm-live")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    presets = [
        LLMPresetConfig(
            id="minimax-cn",
            display_name="Minimax（CN）",
            provider="anthropic",
            base_url="https://api.minimaxi.com/anthropic",
            model="MiniMax-M3",
            api_key_env="CUSTOM_MINIMAX_KEY",
        ),
        LLMPresetConfig(
            id="bigmodel-glm5",
            display_name="智谱 GLM-5.1",
            provider="openai_compatible",
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            model="glm-5.1",
            api_key_env="GLM_API_KEY",
        ),
        LLMPresetConfig(
            id="deepseek",
            display_name="deepseek-v4-flash",
            provider="anthropic",
            base_url="https://api.deepseek.com/anthropic",
            model="deepseek-v4-flash",
            api_key_env="DEEPSEEK_API_KEY",
        ),
    ]
    client = _client(_config(presets))

    resp = client.get("/api/model-providers/model-families")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "providerId": "minimax",
            "providerLabel": "Minimax（CN）",
            "familyId": "minimax:MiniMax-M3",
            "displayName": "MiniMax-M3",
            "presetId": "minimax-cn",
            "model": "MiniMax-M3",
            "connected": True,
        },
        {
            "providerId": "glm",
            "providerLabel": "GLM（CN）",
            "familyId": "glm:glm-5.1",
            "displayName": "glm-5.1",
            "presetId": "bigmodel-glm5",
            "model": "glm-5.1",
            "connected": True,
        },
    ]


def test_model_families_match_custom_proxy_preset_by_model(monkeypatch) -> None:
    monkeypatch.setenv("CUSTOM_MINIMAX_KEY", "minimax-live")
    preset = LLMPresetConfig(
        id="custom-proxy-a",
        display_name="自定义代理",
        provider="anthropic",
        base_url="https://llm-proxy.example.test/anthropic",
        model="MiniMax-M3",
        api_key_env="CUSTOM_MINIMAX_KEY",
    )
    client = _client(_config([preset]))

    resp = client.get("/api/model-providers/model-families")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "providerId": "minimax",
            "providerLabel": "Minimax（CN）",
            "familyId": "minimax:MiniMax-M3",
            "displayName": "MiniMax-M3",
            "presetId": "custom-proxy-a",
            "model": "MiniMax-M3",
            "connected": True,
        }
    ]


def test_model_families_materializes_default_preset_with_default_env(monkeypatch) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", "")
    monkeypatch.setenv("KONGMING_PROVIDER_MINIMAX_API_KEY", "minimax-live")
    cfg = _config()
    client = _client(cfg)

    resp = client.get("/api/model-providers/model-families")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "providerId": "minimax",
            "providerLabel": "Minimax（CN）",
            "familyId": "minimax:MiniMax-M3",
            "displayName": "MiniMax-M3",
            "presetId": "minimax-m3",
            "model": "MiniMax-M3",
            "connected": True,
        }
    ]
    assert [preset.id for preset in cfg.web.llm_presets] == ["minimax-m3"]
    assert [preset.api_key_env for preset in cfg.web.llm_presets] == ["MINIMAX_API_KEY"]
    assert os.environ["MINIMAX_API_KEY"] == "minimax-live"


def test_model_families_syncs_fallback_key_for_existing_default_preset(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", "")
    monkeypatch.setenv("KONGMING_PROVIDER_MINIMAX_API_KEY", "minimax-live")
    preset = LLMPresetConfig(
        id="minimax-m3",
        display_name="Minimax（CN）",
        provider="anthropic",
        base_url="https://api.minimaxi.com/anthropic",
        model="MiniMax-M3",
        api_key_env="MINIMAX_API_KEY",
    )
    client = _client(_config([preset]))

    resp = client.get("/api/model-providers/model-families")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "providerId": "minimax",
            "providerLabel": "Minimax（CN）",
            "familyId": "minimax:MiniMax-M3",
            "displayName": "MiniMax-M3",
            "presetId": "minimax-m3",
            "model": "MiniMax-M3",
            "connected": True,
        }
    ]
    assert os.environ["MINIMAX_API_KEY"] == "minimax-live"


def test_model_families_migrates_old_generic_preset_to_default_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GLM_API_KEY", "")
    monkeypatch.setenv("BIGMODEL_API_KEY", "glm-fallback")
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "generic-key")
    preset = LLMPresetConfig(
        id="bigmodel-glm5",
        display_name="智谱 GLM-5.1",
        provider="openai_compatible",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        model="glm-5.1",
        api_key_env="KONGMING_MODEL_API_KEY",
    )
    cfg = _config([preset])
    client = _client(cfg)

    resp = client.get("/api/model-providers/model-families")

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "providerId": "glm",
            "providerLabel": "GLM（CN）",
            "familyId": "glm:glm-5.1",
            "displayName": "glm-5.1",
            "presetId": "bigmodel-glm5",
            "model": "glm-5.1",
            "connected": True,
        }
    ]
    assert [preset.api_key_env for preset in cfg.web.llm_presets] == ["GLM_API_KEY"]
    assert os.environ["GLM_API_KEY"] == "glm-fallback"


def test_current_probe_uses_one_token_anthropic_request(monkeypatch) -> None:
    monkeypatch.setenv("KONGMING_PROVIDER_MINIMAX_API_KEY", "sk-live")
    captured: dict[str, object] = {}
    real_async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"id": "msg_1"})

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout
            self._client = real_async_client(transport=httpx.MockTransport(handler))

        async def __aenter__(self) -> httpx.AsyncClient:
            return self._client

        async def __aexit__(self, exc_type, exc, tb) -> None:
            await self._client.aclose()

    monkeypatch.setattr(router_mod.httpx, "AsyncClient", FakeAsyncClient)
    client = _client(_config())

    resp = client.post("/api/model-providers/minimax/test-current")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["message"] == "连接测试通过。"
    assert captured["url"] == "https://api.minimaxi.com/anthropic/v1/messages"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer sk-live"
    assert headers["anthropic-version"] == "2023-06-01"
    assert captured["timeout"] == 15.0
    assert '"max_tokens":1' in str(captured["json"]).replace(" ", "")
    assert '"content":"ping"' in str(captured["json"]).replace(" ", "")


def test_current_probe_uses_one_token_openai_request(monkeypatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "glm-live")
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "generic-live")
    captured: dict[str, object] = {}
    real_async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"id": "chatcmpl_1"})

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout
            self._client = real_async_client(transport=httpx.MockTransport(handler))

        async def __aenter__(self) -> httpx.AsyncClient:
            return self._client

        async def __aexit__(self, exc_type, exc, tb) -> None:
            await self._client.aclose()

    monkeypatch.setattr(router_mod.httpx, "AsyncClient", FakeAsyncClient)
    preset = LLMPresetConfig(
        id="bigmodel-glm5",
        display_name="智谱 GLM-5.1",
        provider="openai_compatible",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        model="glm-5.1",
        api_key_env="GLM_API_KEY",
    )
    client = _client(_config([preset]))

    resp = client.post("/api/model-providers/glm/test-current")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert captured["url"] == ("https://open.bigmodel.cn/api/coding/paas/v4/chat/completions")
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer glm-live"
    assert "anthropic-version" not in headers
    assert captured["timeout"] == 15.0
    assert '"max_tokens":1' in str(captured["json"]).replace(" ", "")
    assert '"content":"ping"' in str(captured["json"]).replace(" ", "")


def test_current_probe_uses_glm_fallback_key(monkeypatch) -> None:
    monkeypatch.setenv("ZHIPU_API_KEY", "glm-fallback")
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "generic-live")
    captured: dict[str, object] = {}
    real_async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"id": "chatcmpl_1"})

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self._client = real_async_client(transport=httpx.MockTransport(handler))

        async def __aenter__(self) -> httpx.AsyncClient:
            return self._client

        async def __aexit__(self, exc_type, exc, tb) -> None:
            await self._client.aclose()

    monkeypatch.setattr(router_mod.httpx, "AsyncClient", FakeAsyncClient)
    client = _client(_config())

    resp = client.post("/api/model-providers/glm/test-current")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer glm-fallback"


def test_current_probe_missing_key_message_lists_provider_envs(monkeypatch) -> None:
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "generic-live")
    preset = LLMPresetConfig(
        id="bigmodel-glm5",
        display_name="智谱 GLM-5.1",
        provider="openai_compatible",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        model="glm-5.1",
        api_key_env="KONGMING_MODEL_API_KEY",
    )
    client = _client(_config([preset]))

    resp = client.post("/api/model-providers/glm/test-current")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["message"] == (
        "未找到 GLM API Key；请配置 GLM_API_KEY / BIGMODEL_API_KEY / ZHIPU_API_KEY / ZAI_API_KEY。"
    )
    assert "KONGMING_MODEL_API_KEY" not in body["message"]


def test_draft_probe_uses_request_api_key(monkeypatch) -> None:
    captured: dict[str, object] = {}
    real_async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["json"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"id": "msg_1"})

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout
            self._client = real_async_client(transport=httpx.MockTransport(handler))

        async def __aenter__(self) -> httpx.AsyncClient:
            return self._client

        async def __aexit__(self, exc_type, exc, tb) -> None:
            await self._client.aclose()

    monkeypatch.setattr(router_mod.httpx, "AsyncClient", FakeAsyncClient)
    client = _client(_config())

    resp = client.post("/api/model-providers/minimax/test", json={"apiKey": "sk-draft"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer sk-draft"
    assert captured["timeout"] == 15.0
    assert '"max_tokens":1' in str(captured["json"]).replace(" ", "")
