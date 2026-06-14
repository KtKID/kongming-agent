"""模型服务商管理 API。

Catalog 固定声明 Web 管理页首批支持的服务商；连接状态从当前进程
``os.environ`` 与 ``app.state.config.web.llm_presets`` 推断。测试接口发最小
probe 请求，避免管理页测试按钮触发长回复。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel

from infrastructure.config.models import LLMPresetConfig

if TYPE_CHECKING:
    from infrastructure.config.models import Config

router = APIRouter(prefix="/api/model-providers", tags=["model-providers"])

PROVIDER_TEST_TIMEOUT_SECONDS = 15.0
PROVIDER_TEST_MAX_TOKENS = 1
GENERIC_MODEL_API_KEY_ENV = "KONGMING_MODEL_API_KEY"


@dataclass(frozen=True)
class _ProviderDefinition:
    provider_id: str
    default_preset_id: str
    display_name: str
    region_label: str
    description: str
    logo_text: str
    default_api_key_env: str
    fallback_api_key_envs: tuple[str, ...]
    default_base_url: str
    default_model: str
    protocol: Literal["anthropic", "openai"]
    match_keywords: tuple[str, ...]
    match_hosts: tuple[str, ...]


PROVIDER_DEFINITIONS: tuple[_ProviderDefinition, ...] = (
    _ProviderDefinition(
        provider_id="minimax",
        default_preset_id="minimax-m3",
        display_name="Minimax",
        region_label="CN",
        description="中国区 Minimax API Key，用于启用对应模型预设。",
        logo_text="M",
        default_api_key_env="MINIMAX_API_KEY",
        fallback_api_key_envs=("KONGMING_PROVIDER_MINIMAX_API_KEY",),
        default_base_url="https://api.minimaxi.com/anthropic",
        default_model="MiniMax-M3",
        protocol="anthropic",
        match_keywords=("minimax", "mini max"),
        match_hosts=("api.minimaxi.com",),
    ),
    _ProviderDefinition(
        provider_id="glm",
        default_preset_id="bigmodel-glm5",
        display_name="GLM",
        region_label="CN",
        description="智谱 GLM API Key，用于启用 GLM 模型预设。",
        logo_text="G",
        default_api_key_env="GLM_API_KEY",
        fallback_api_key_envs=("BIGMODEL_API_KEY", "ZHIPU_API_KEY", "ZAI_API_KEY"),
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-5.1",
        protocol="openai",
        match_keywords=("glm", "bigmodel", "智谱"),
        match_hosts=("open.bigmodel.cn",),
    ),
    _ProviderDefinition(
        provider_id="deepseek",
        default_preset_id="deepseek",
        display_name="DeepSeek",
        region_label="CN",
        description="DeepSeek API Key，用于启用 DeepSeek 模型预设。",
        logo_text="D",
        default_api_key_env="DEEPSEEK_API_KEY",
        fallback_api_key_envs=(),
        default_base_url="https://api.deepseek.com/anthropic",
        default_model="deepseek-v4-flash",
        protocol="anthropic",
        match_keywords=("deepseek", "deep seek"),
        match_hosts=("api.deepseek.com",),
    ),
)


@dataclass(frozen=True)
class ProviderCatalogItemDTO:
    providerId: str
    displayName: str
    regionLabel: str
    description: str
    logoText: str


@dataclass(frozen=True)
class ProviderConnectionDTO:
    providerId: str
    status: Literal["connected", "disconnected", "error"]
    model: str | None
    authLabel: str | None


@dataclass(frozen=True)
class ConnectedModelFamilyDTO:
    providerId: str
    providerLabel: str
    familyId: str
    displayName: str
    presetId: str
    model: str
    connected: bool


@dataclass(frozen=True)
class ProviderActionResponseDTO:
    providerId: str
    ok: bool
    message: str
    connection: ProviderConnectionDTO | None = None


class TestProviderRequest(BaseModel):
    apiKey: str | None = None


class ConnectProviderRequest(BaseModel):
    apiKey: str | None = None


@router.get("/catalog")
async def list_provider_catalog() -> list[ProviderCatalogItemDTO]:
    """返回首批支持的模型服务商目录。"""
    return [
        ProviderCatalogItemDTO(
            providerId=definition.provider_id,
            displayName=definition.display_name,
            regionLabel=definition.region_label,
            description=definition.description,
            logoText=definition.logo_text,
        )
        for definition in PROVIDER_DEFINITIONS
    ]


@router.post("/{provider_id}/test")
async def test_provider_connection(
    provider_id: str,
    body: TestProviderRequest,
    request: Request,
) -> ProviderActionResponseDTO:
    """测试用户正在输入的 API Key。

    探测请求固定 `max_tokens=1`，只发送 `ping`，避免测试按钮生成长回复。
    """
    definition = _provider_definition(provider_id)
    if definition is None:
        return ProviderActionResponseDTO(
            providerId=provider_id,
            ok=False,
            message="未知模型服务商。",
        )
    api_key = (body.apiKey or "").strip()
    if not api_key:
        return ProviderActionResponseDTO(
            providerId=provider_id,
            ok=False,
            message="请输入 API Key。",
        )

    cfg: Config = request.app.state.config
    preset = _find_provider_preset(definition, cfg.web.llm_presets)
    result = await _probe_provider(definition=definition, api_key=api_key, preset=preset)
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
    """保存已测试通过的 API Key 到受控 env，并刷新当前进程连接状态。"""
    definition = _provider_definition(provider_id)
    if definition is None:
        return ProviderActionResponseDTO(
            providerId=provider_id,
            ok=False,
            message="未知模型服务商。",
        )
    api_key = (body.apiKey or "").strip()
    if not api_key:
        return ProviderActionResponseDTO(
            providerId=provider_id,
            ok=False,
            message="请输入 API Key。",
        )

    cfg: Config = request.app.state.config
    preset = _find_provider_preset(definition, cfg.web.llm_presets)
    env_name = definition.default_api_key_env
    config_manager = getattr(request.app.state, "config_manager", None)
    if config_manager is not None:
        config_manager.write_env_values({env_name: api_key})
    else:
        os.environ[env_name] = api_key

    if preset is None:
        preset = _default_preset_for_provider(definition, env_name)
        if config_manager is not None:
            config_manager.upsert_web_llm_preset(preset)
        _attach_runtime_preset(cfg, preset)
    elif preset.api_key_env != env_name:
        preset = preset.model_copy(update={"api_key_env": env_name})
        if config_manager is not None:
            config_manager.upsert_web_llm_preset(preset)
        _attach_runtime_preset(cfg, preset)

    connection = _provider_connection_from_env(definition, preset)
    return ProviderActionResponseDTO(
        providerId=provider_id,
        ok=True,
        message="已保存，刚刚测试通过。",
        connection=connection,
    )


@router.post("/{provider_id}/test-current")
async def test_current_provider_connection(
    provider_id: str,
    request: Request,
) -> ProviderActionResponseDTO:
    """测试当前已保存的模型服务商连接。"""
    definition = _provider_definition(provider_id)
    if definition is None:
        return ProviderActionResponseDTO(
            providerId=provider_id,
            ok=False,
            message="未知模型服务商。",
        )

    cfg: Config = request.app.state.config
    preset = _find_provider_preset(definition, cfg.web.llm_presets)
    api_key = _resolve_api_key(definition, preset)
    if not api_key.value:
        return ProviderActionResponseDTO(
            providerId=provider_id,
            ok=False,
            message=_missing_api_key_message(definition, preset),
            connection=_provider_connection_from_env(definition, preset),
        )

    result = await _probe_provider(
        definition=definition,
        api_key=api_key.value,
        preset=preset,
    )
    return ProviderActionResponseDTO(
        providerId=provider_id,
        ok=result.ok,
        message=result.message,
        connection=_provider_connection_from_env(definition, preset),
    )


@router.get("/connections")
async def list_provider_connections(request: Request) -> list[ProviderConnectionDTO]:
    """返回模型服务商连接状态。

    已连接判定：匹配到对应 `web.llm_presets` 时读取 preset 的
    `api_key_env`；没有匹配 preset 时读取 provider 默认 env 名。
    """
    cfg: Config = request.app.state.config
    return [
        _provider_connection_from_env(
            definition,
            _find_provider_preset(definition, cfg.web.llm_presets),
        )
        for definition in PROVIDER_DEFINITIONS
    ]


@router.get("/model-families")
async def list_connected_model_families(request: Request) -> list[ConnectedModelFamilyDTO]:
    """返回 Composer 可切换的已连接模型家族。

    只有同时满足“服务商已连接”和“能解析到真实 preset_id”的项才返回。
    前端通过该 DTO 获取可切换模型，不直接读取 YAML、env 或 preset 命名。
    """
    cfg: Config = request.app.state.config
    families: list[ConnectedModelFamilyDTO] = []
    for definition in PROVIDER_DEFINITIONS:
        preset = _find_provider_preset(definition, cfg.web.llm_presets)
        if preset is None:
            continue
        api_key = _resolve_api_key(definition, preset)
        if not _preset_runtime_can_read_key(preset, api_key):
            continue
        connection = _provider_connection_from_env(definition, preset)
        if connection.status != "connected":
            continue
        families.append(_connected_model_family(definition, preset))
    return families


@dataclass(frozen=True)
class _ProbeResult:
    ok: bool
    message: str


async def _probe_provider(
    *,
    definition: _ProviderDefinition,
    api_key: str,
    preset: LLMPresetConfig | None,
) -> _ProbeResult:
    """向对应服务商发最小探测请求。"""
    base_url = preset.base_url if preset is not None else definition.default_base_url
    model = preset.model if preset is not None else definition.default_model
    if definition.protocol == "openai":
        url = _openai_chat_url(base_url)
        payload = {
            "model": model,
            "max_tokens": PROVIDER_TEST_MAX_TOKENS,
            "messages": [{"role": "user", "content": "ping"}],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
    else:
        url = _anthropic_messages_url(base_url)
        payload = {
            "model": model,
            "max_tokens": PROVIDER_TEST_MAX_TOKENS,
            "messages": [{"role": "user", "content": "ping"}],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    try:
        async with httpx.AsyncClient(timeout=PROVIDER_TEST_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        return _ProbeResult(ok=False, message="连接测试超时。")
    except httpx.HTTPError as exc:
        return _ProbeResult(ok=False, message=f"连接测试失败：{exc}")

    if response.status_code < 400:
        return _ProbeResult(ok=True, message="连接测试通过。")
    return _ProbeResult(
        ok=False,
        message=f"连接测试失败：HTTP {response.status_code}",
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


def _provider_connection_from_env(
    definition: _ProviderDefinition,
    preset: LLMPresetConfig | None,
) -> ProviderConnectionDTO:
    """按当前 env 与 preset 推断服务商连接 DTO。"""
    connected = bool(_resolve_api_key(definition, preset).value)
    return ProviderConnectionDTO(
        providerId=definition.provider_id,
        status="connected" if connected else "disconnected",
        model=preset.model if preset is not None else None,
        authLabel="Bearer" if connected else None,
    )


def _connected_model_family(
    definition: _ProviderDefinition,
    preset: LLMPresetConfig,
) -> ConnectedModelFamilyDTO:
    """把 provider preset 投影成 Composer 模型家族选项。"""
    model = preset.model or definition.default_model
    return ConnectedModelFamilyDTO(
        providerId=definition.provider_id,
        providerLabel=f"{definition.display_name}（{definition.region_label}）",
        familyId=f"{definition.provider_id}:{model}",
        displayName=_family_display_name(definition, model),
        presetId=preset.id,
        model=model,
        connected=True,
    )


def _family_display_name(definition: _ProviderDefinition, model: str) -> str:
    """返回模型切换菜单展示名，避免前端硬编码服务商文案。"""
    if definition.provider_id == "minimax":
        return "MiniMax-M3" if model == "MiniMax-M3" else f"MiniMax-{model}"
    if definition.provider_id == "glm":
        return "glm-5.1" if model == "glm-5.1" else model
    return model


def _default_preset_for_provider(
    definition: _ProviderDefinition,
    api_key_env: str,
) -> LLMPresetConfig:
    """把服务商默认信息转换为 Web 可选模型 preset。"""
    provider: Literal["anthropic", "openai_compatible"] = (
        "anthropic" if definition.protocol == "anthropic" else "openai_compatible"
    )
    return LLMPresetConfig(
        id=definition.default_preset_id,
        display_name=f"{definition.display_name}（{definition.region_label}）",
        provider=provider,
        base_url=definition.default_base_url,
        model=definition.default_model,
        api_key_env=api_key_env,
        reasoning_effort="high",
    )


def _attach_runtime_preset(cfg: Config, preset: LLMPresetConfig) -> None:
    """连接成功后同步当前进程配置，让菜单刷新立即看到新模型。"""
    presets = list(getattr(cfg.web, "llm_presets", []) or [])
    for idx, item in enumerate(presets):
        if item.id == preset.id:
            presets[idx] = preset
            break
    else:
        presets.append(preset)

    if hasattr(cfg.web, "model_copy"):
        cfg.web = cfg.web.model_copy(update={"llm_presets": presets})
    else:
        cfg.web.llm_presets = presets


@dataclass(frozen=True)
class _ApiKeyResolution:
    env_name: str | None
    value: str


def _resolve_api_key(
    definition: _ProviderDefinition,
    preset: LLMPresetConfig | None,
) -> _ApiKeyResolution:
    """按服务商候选 env 顺序读取 API Key。"""
    for env_name in _api_key_env_candidates(definition, preset):
        value = os.environ.get(env_name, "").strip()
        if value:
            return _ApiKeyResolution(env_name=env_name, value=value)
    return _ApiKeyResolution(env_name=None, value="")


def _preset_runtime_can_read_key(
    preset: LLMPresetConfig,
    api_key: _ApiKeyResolution,
) -> bool:
    """判断返回的 preset 在运行时工厂中能直接读到 key。"""
    return bool(
        api_key.value
        and preset.api_key_env
        and api_key.env_name == preset.api_key_env
        and preset.api_key_env != GENERIC_MODEL_API_KEY_ENV
    )


def _api_key_env_candidates(
    definition: _ProviderDefinition,
    preset: LLMPresetConfig | None,
) -> list[str]:
    """返回去重后的 API Key env 候选列表。"""
    candidates = [
        definition.default_api_key_env,
        *definition.fallback_api_key_envs,
    ]
    result: list[str] = []
    for env_name in candidates:
        if env_name and env_name not in result:
            result.append(env_name)
    return result


def _missing_api_key_message(
    definition: _ProviderDefinition,
    preset: LLMPresetConfig | None,
) -> str:
    """返回缺 key 提示。"""
    env_names = [definition.default_api_key_env, *definition.fallback_api_key_envs]
    if (
        preset is not None
        and preset.api_key_env
        and preset.api_key_env != GENERIC_MODEL_API_KEY_ENV
    ):
        env_names.append(preset.api_key_env)
    unique_env_names = list(dict.fromkeys(name for name in env_names if name))
    return f"未找到 {definition.display_name} API Key；请配置 {' / '.join(unique_env_names)}。"


def _provider_definition(provider_id: str) -> _ProviderDefinition | None:
    """按 provider id 读取目录定义。"""
    normalized = provider_id.lower()
    return next(
        (definition for definition in PROVIDER_DEFINITIONS if definition.provider_id == normalized),
        None,
    )


def _find_provider_preset(
    definition: _ProviderDefinition,
    presets: list[LLMPresetConfig],
) -> LLMPresetConfig | None:
    """从 web preset 列表中识别指定服务商 preset。"""
    for preset in presets:
        preset_id = preset.id.lower()
        display_name = preset.display_name.lower()
        base_url = preset.base_url.lower()
        if preset_id == definition.provider_id:
            return preset
        if any(keyword in preset_id for keyword in definition.match_keywords):
            return preset
        if any(keyword in display_name for keyword in definition.match_keywords):
            return preset
        if any(host in base_url for host in definition.match_hosts):
            return preset
        if preset.model.lower() == definition.default_model.lower():
            return preset
    return None


__all__ = ["router"]
