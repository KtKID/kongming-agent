"""客户端配置下发（公开给浏览器的非敏感运行参数）。

端点：

- ``GET /api/config/client`` — 返回前端所需的心跳参数

这些配置由后端 ``WebConfig`` 统一管理，前端不硬编码，启动时一次拉取即可。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

if TYPE_CHECKING:
    from infrastructure.config.models import Config

router = APIRouter(prefix="/api/config", tags=["config"])


class ClientCapabilitiesDTO(BaseModel):
    """下发给前端的宿主能力集合。"""

    xspace_host: bool
    native_file_dialog: bool


class ClientConfigDTO(BaseModel):
    """下发给前端的客户端配置。"""

    host_environment: Literal["browser", "xspace"]
    capabilities: ClientCapabilitiesDTO
    ws_heartbeat_interval_ms: int
    ws_heartbeat_background_interval_ms: int
    ws_heartbeat_timeout_ms: int
    ws_heartbeat_max_missed: int
    dashboard_poll_interval_seconds: int
    timezone: str


@router.get("/client")
async def get_client_config(request: Request) -> ClientConfigDTO:
    """返回前端所需的心跳等运行参数。"""
    cfg: Config = request.app.state.config
    web = cfg.web
    xspace_host = web.host_environment == "xspace"
    return ClientConfigDTO(
        host_environment=web.host_environment,
        capabilities=ClientCapabilitiesDTO(
            xspace_host=xspace_host,
            native_file_dialog=xspace_host,
        ),
        ws_heartbeat_interval_ms=web.ws_heartbeat_interval_ms,
        ws_heartbeat_background_interval_ms=web.ws_heartbeat_background_interval_ms,
        ws_heartbeat_timeout_ms=web.ws_heartbeat_timeout_ms,
        ws_heartbeat_max_missed=web.ws_heartbeat_max_missed,
        dashboard_poll_interval_seconds=web.normalized_dashboard_poll_interval_seconds,
        timezone=cfg.scheduler.default_timezone,
    )


__all__ = ["router"]
