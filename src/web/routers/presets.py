"""LLM preset 列表（脱敏，公开给浏览器）。

端点：

- ``GET /api/presets`` — 返回 :class:`web.protocol.LLMPresetDTO` 列表

脱敏要点：

- **不**返回 ``api_key``；只返回 ``requires_api_key: bool``（即 preset 有
  ``api_key_env`` 字段）。
- ``base_url_summary`` 只保留 host:port 部分，不暴露 path / query。
  - 本地端点（127.x / localhost）保留完整 URL（无敏感信息）
  - 远端端点只保留 host 部分（``api.openai.com``）

依赖：``cfg.web.llm_presets`` 列表。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastapi import APIRouter, Request

from web.protocol import LLMPresetDTO

if TYPE_CHECKING:
    from infrastructure.config.models import Config, LLMPresetConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/presets", tags=["presets"])


_LOCAL_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "host.docker.internal",
    }
)


def _is_local(host: str) -> bool:
    host_lower = host.lower()
    if host_lower in _LOCAL_HOSTS:
        return True
    return host_lower.startswith("127.")


def _redact_base_url(base_url: str) -> str:
    """脱敏 base_url：只保留 host[:port]；本地原样回。

    实现：

    - 本地 host → 完整 URL（含 path）；本地无敏感信息，便于前端直连诊断
    - 远端 host → ``host[:port]``（无 scheme / path）
    - URL 解析失败 → 原样返回（防御）
    """
    try:
        parsed = urlparse(base_url)
    except Exception:
        return base_url
    host = parsed.hostname
    if not host:
        return base_url
    if _is_local(host):
        return base_url
    if parsed.port is not None:
        return f"{host}:{parsed.port}"
    return host


def _preset_to_dto(preset: LLMPresetConfig) -> LLMPresetDTO:
    return LLMPresetDTO(
        id=preset.id,
        display_name=preset.display_name,
        model=preset.model,
        base_url_summary=_redact_base_url(preset.base_url),
        requires_api_key=preset.api_key_env is not None,
    )


@router.get("")
async def list_presets(request: Request) -> list[LLMPresetDTO]:
    """返回所有可用 preset（脱敏）。"""
    cfg: Config = request.app.state.config
    return [_preset_to_dto(p) for p in cfg.web.llm_presets]


__all__ = ["router"]
