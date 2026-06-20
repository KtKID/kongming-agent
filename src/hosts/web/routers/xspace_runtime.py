"""XSpace 宿主启动 runtime 初始化接口。

本路由服务 XSpace native 宿主在 Web sidecar ready 后、WebView 加载前
写入当前进程的宿主运行态。接口只更新内存态，不写入 setting.yaml。
"""

from __future__ import annotations

import os
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel

_WEB_HOST_ENVIRONMENT_ENV = "KONGMING_WEB_HOST_ENVIRONMENT"

router = APIRouter(prefix="/api/xspace/runtime", tags=["xspace-runtime"])


class XSpaceRuntimeInitRequest(BaseModel):
    """XSpace 启动初始化请求。"""

    host_environment: Literal["xspace"] = "xspace"


class XSpaceRuntimeInitResponse(BaseModel):
    """XSpace 启动初始化响应。"""

    host_environment: Literal["xspace"]
    config_client_path: str


@router.post("/init")
async def init_xspace_runtime(
    request: Request,
    payload: Annotated[XSpaceRuntimeInitRequest | None, Body()] = None,
) -> XSpaceRuntimeInitResponse:
    """把当前 Web sidecar 进程标记为 XSpace 宿主。

    Args:
        request: FastAPI 请求对象，提供 app.state.config。
        payload: 初始化请求；为空时默认设置为 ``xspace``。

    Returns:
        当前宿主环境和前端配置端点路径。
    """
    body = payload or XSpaceRuntimeInitRequest()
    os.environ[_WEB_HOST_ENVIRONMENT_ENV] = body.host_environment

    cfg = request.app.state.config
    web_cfg = cfg.web.model_copy(update={"host_environment": body.host_environment})
    request.app.state.config = cfg.model_copy(update={"web": web_cfg})

    return XSpaceRuntimeInitResponse(
        host_environment=body.host_environment,
        config_client_path="/api/config/client",
    )


__all__ = [
    "XSpaceRuntimeInitRequest",
    "XSpaceRuntimeInitResponse",
    "router",
]
