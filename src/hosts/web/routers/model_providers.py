"""模型服务商管理 API，所有静态定义与能力均投影自 ModelCatalogManager。"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Request

from hosts.web.protocol import (
    ConnectedModelFamilyDTO,
    ConnectProviderRequest,
    ProviderActionResponseDTO,
    ProviderCatalogItemDTO,
    ProviderConnectionDTO,
    ReasoningEffort,
    TestProviderRequest,
)
from infrastructure.config.api_key_headers import api_key_header_label, build_api_key_headers
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.model_provider_catalog import (
    ModelProviderDefinition,
    ModelProviderModelDefinition,
    ProviderProtocol,
    ReasoningAdapter,
    ReasoningCapability,
)
from infrastructure.config.models import ApiKeyHeader

router = APIRouter(prefix="/api/model-providers", tags=["model-providers"])

PROVIDER_TEST_TIMEOUT_SECONDS = 15.0
PROVIDER_TEST_MAX_TOKENS = 1


@dataclass(frozen=True)
class _ProbeResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class _ApiKeyResolution:
    env_name: str | None
    value: str


def _manager(request: Request) -> ModelCatalogManager:
    """返回 Web composition root 注入的 catalog Manager。"""
    manager = getattr(request.app.state, "model_catalog_manager", None)
    if isinstance(manager, ModelCatalogManager):
        return manager
    return ModelCatalogManager()


def _definition(
    manager: ModelCatalogManager,
    provider_id: str,
) -> ModelProviderDefinition | None:
    """按 provider ID 查询合并后的目录定义。"""
    normalized = provider_id.lower()
    return next(
        (
            definition
            for definition in manager.list_providers()
            if definition.provider_id.lower() == normalized
        ),
        None,
    )


def _default_model(definition: ModelProviderDefinition) -> ModelProviderModelDefinition:
    """返回 provider 的默认模型。"""
    return next(
        model for model in definition.models if model.preset_id == definition.default_preset_id
    )


@router.get("/catalog")
async def list_provider_catalog(request: Request) -> list[ProviderCatalogItemDTO]:
    """返回合并 catalog 的 provider 列表。"""
    return [
        ProviderCatalogItemDTO(
            providerId=definition.provider_id,
            displayName=definition.display_name,
            regionLabel=definition.region_label,
            description=definition.description,
            logoText=definition.logo_text,
        )
        for definition in _manager(request).list_providers()
    ]


@router.post("/{provider_id}/test")
async def test_provider_connection(
    provider_id: str,
    body: TestProviderRequest,
    request: Request,
) -> ProviderActionResponseDTO:
    """使用用户输入 credential 发出最小 provider probe。"""
    definition = _definition(_manager(request), provider_id)
    if definition is None:
        return _unknown_provider(provider_id)
    api_key = (body.apiKey or "").strip()
    if not api_key:
        return ProviderActionResponseDTO(
            providerId=provider_id, ok=False, message="请输入 API Key。"
        )
    result = await _probe_provider(
        definition=definition,
        model=_default_model(definition),
        api_key=api_key,
    )
    return ProviderActionResponseDTO(
        providerId=provider_id,
        ok=result.ok,
        message=result.message,
    )


@router.post("/{provider_id}/connect")
async def connect_provider(
    provider_id: str,
    body: ConnectProviderRequest,
    request: Request,
) -> ProviderActionResponseDTO:
    """保存 provider-specific credential env，catalog 定义保持只读。"""
    definition = _definition(_manager(request), provider_id)
    if definition is None:
        return _unknown_provider(provider_id)
    env_name = definition.default_api_key_env
    api_key = (body.apiKey or "").strip()
    if env_name is None:
        return ProviderActionResponseDTO(
            providerId=provider_id,
            ok=True,
            message="本地模型无需 API Key。",
            connection=_provider_connection(definition),
        )
    if not api_key:
        return ProviderActionResponseDTO(
            providerId=provider_id, ok=False, message="请输入 API Key。"
        )
    config_manager = getattr(request.app.state, "config_manager", None)
    if config_manager is not None:
        config_manager.write_env_values({env_name: api_key})
    else:
        os.environ[env_name] = api_key
    return ProviderActionResponseDTO(
        providerId=provider_id,
        ok=True,
        message="已保存，刚刚测试通过。",
        connection=_provider_connection(definition),
    )


@router.post("/{provider_id}/test-current")
async def test_current_provider_connection(
    provider_id: str,
    request: Request,
) -> ProviderActionResponseDTO:
    """测试当前 provider-specific credential。"""
    definition = _definition(_manager(request), provider_id)
    if definition is None:
        return _unknown_provider(provider_id)
    api_key = _resolve_api_key(definition)
    if not api_key.value and definition.default_api_key_env is not None:
        return ProviderActionResponseDTO(
            providerId=provider_id,
            ok=False,
            message=_missing_api_key_message(definition),
            connection=_provider_connection(definition),
        )
    result = await _probe_provider(
        definition=definition,
        model=_default_model(definition),
        api_key=api_key.value,
    )
    return ProviderActionResponseDTO(
        providerId=provider_id,
        ok=result.ok,
        message=result.message,
        connection=_provider_connection(definition),
    )


@router.get("/connections")
async def list_provider_connections(request: Request) -> list[ProviderConnectionDTO]:
    """返回 credential 引用派生的 provider 连接状态。"""
    return [_provider_connection(definition) for definition in _manager(request).list_providers()]


@router.get("/model-families")
async def list_connected_model_families(request: Request) -> list[ConnectedModelFamilyDTO]:
    """从同一 catalog 投影已连接模型与 reasoning/context capability。"""
    families: list[ConnectedModelFamilyDTO] = []
    for definition in _manager(request).list_providers():
        connection = _provider_connection(definition)
        if connection.status != "connected":
            continue
        for model in definition.models:
            capability = model.reasoning
            families.append(
                ConnectedModelFamilyDTO(
                    providerId=definition.provider_id,
                    providerLabel=f"{definition.display_name}（{definition.region_label}）",
                    familyId=f"{definition.provider_id}:{model.model}",
                    displayName=model.display_name or model.model,
                    presetId=model.preset_id,
                    model=model.model,
                    connected=True,
                    supportedReasoningEfforts=_supported_reasoning_efforts(capability),
                    defaultReasoningEffort=(
                        capability.default_effort if capability is not None else None
                    ),
                    reasoningAdapter=(capability.adapter.value if capability is not None else None),
                    contextWindowTokens=model.context_window_tokens,
                )
            )
    return families


def _unknown_provider(provider_id: str) -> ProviderActionResponseDTO:
    """返回稳定未知 provider 响应。"""
    return ProviderActionResponseDTO(
        providerId=provider_id,
        ok=False,
        message="未知模型服务商。",
    )


def _supported_reasoning_efforts(
    capability: ReasoningCapability | None,
) -> list[ReasoningEffort]:
    """返回 UI 可直接展示的档位；空列表表示隐藏 reasoning 控件。"""
    if capability is None or capability.adapter is ReasoningAdapter.NONE:
        return []
    efforts: list[ReasoningEffort] = list(capability.supported_efforts)
    if capability.supports_disabled:
        return ["none", *efforts]
    return efforts


def _provider_connection(definition: ModelProviderDefinition) -> ProviderConnectionDTO:
    """按 provider credential env 和本地 endpoint 推导连接 DTO。"""
    api_key = _resolve_api_key(definition)
    model = _default_model(definition)
    local = _is_local_endpoint(model.base_url or definition.default_base_url)
    connected = bool(api_key.value) or local
    header = _api_key_header(definition, model)
    return ProviderConnectionDTO(
        providerId=definition.provider_id,
        status="connected" if connected else "disconnected",
        model=model.model if connected else None,
        authLabel=(api_key_header_label(header) if api_key.value else None),
    )


def _resolve_api_key(definition: ModelProviderDefinition) -> _ApiKeyResolution:
    """按 catalog credential env 候选顺序读取值。"""
    candidates = (
        definition.default_api_key_env,
        *definition.fallback_api_key_envs,
    )
    for env_name in candidates:
        if env_name is None:
            continue
        value = os.environ.get(env_name, "").strip()
        if value:
            return _ApiKeyResolution(env_name=env_name, value=value)
    return _ApiKeyResolution(env_name=None, value="")


def _api_key_header(
    definition: ModelProviderDefinition,
    model: ModelProviderModelDefinition,
) -> ApiKeyHeader:
    """返回 probe 使用的鉴权 header。"""
    if model.api_key_header is not None:
        return model.api_key_header
    if definition.default_api_key_header is not None:
        return definition.default_api_key_header
    if definition.protocol is ProviderProtocol.ANTHROPIC:
        return "x-api-key"
    return "authorization-bearer"


def _missing_api_key_message(definition: ModelProviderDefinition) -> str:
    """返回 provider credential 缺失诊断。"""
    env_names = [
        env_name
        for env_name in (
            definition.default_api_key_env,
            *definition.fallback_api_key_envs,
        )
        if env_name is not None
    ]
    return f"未找到 {definition.display_name} API Key；请配置 {' / '.join(env_names)}。"


async def _probe_provider(
    *,
    definition: ModelProviderDefinition,
    model: ModelProviderModelDefinition,
    api_key: str,
) -> _ProbeResult:
    """向 catalog model 发出固定 max_tokens=1 的最小请求。"""
    base_url = model.base_url or definition.default_base_url
    payload = {
        "model": model.model,
        "max_tokens": PROVIDER_TEST_MAX_TOKENS,
        "messages": [{"role": "user", "content": "ping"}],
    }
    header = _api_key_header(definition, model)
    headers = {
        **build_api_key_headers(api_key=api_key, api_key_header=header),
        "content-type": "application/json",
    }
    if definition.protocol is ProviderProtocol.ANTHROPIC:
        url = _anthropic_messages_url(base_url)
        headers["anthropic-version"] = "2023-06-01"
    else:
        url = _openai_chat_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=PROVIDER_TEST_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        return _ProbeResult(ok=False, message="连接测试超时。")
    except httpx.HTTPError as exc:
        return _ProbeResult(ok=False, message=f"连接测试失败：{exc}")
    if response.status_code < 400:
        return _ProbeResult(ok=True, message="连接测试通过。")
    return _ProbeResult(ok=False, message=f"连接测试失败：HTTP {response.status_code}")


def _is_local_endpoint(base_url: str) -> bool:
    """判断 endpoint 是否为本地地址。"""
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").lower()
    return host in {"localhost", "0.0.0.0", "::1", "host.docker.internal"} or host.startswith(
        "127."
    )


def _anthropic_messages_url(base_url: str) -> str:
    """把 Anthropic 兼容 base_url 归一到 messages endpoint。"""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1/messages"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/messages"
    return f"{normalized}/v1/messages"


def _openai_chat_url(base_url: str) -> str:
    """把 OpenAI 兼容 base_url 归一到 chat completions endpoint。"""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


__all__ = ["router"]
