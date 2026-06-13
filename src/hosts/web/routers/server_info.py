"""Server info 路由（v0.1，web-projects-registry-v0.1 #7）。

端点：

- ``GET /api/server/info`` — 返回 web server 进程的 ``_REPO_ROOT`` 绝对路径，
  供前端 "📍 一键填当前 worktree" 按钮使用。

设计要点：

- ``repo_root`` 计算独立于 ``web.ctl``，避免引入 ``ctl.py`` 的 ``load_dotenv``
  等启动副作用。本文件直接走 ``Path(__file__).resolve().parents[3]``：
  ``src/hosts/web/routers/server_info.py`` → parents[3] = 项目根。
- DTO 走 :class:`web.protocol.rest_models.ServerInfoResponse`，与协议层保持一致。
- 鉴权交给全局 ``web.auth.middleware.AuthMiddleware`` 兜底，本路由不自持 Depends。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from hosts.web.protocol.rest_models import ServerInfoResponse

# src/hosts/web/routers/server_info.py → parents[3] 指向项目根 (kongming-agent/)。
# 与 ``web.ctl._REPO_ROOT`` (parents[2]) 在不同层级；不要从 ctl 直接 import，
# 避免引入 dotenv 启动副作用。
_REPO_ROOT: Path = Path(__file__).resolve().parents[3]


router = APIRouter(prefix="/api/server", tags=["server"])


@router.get("/info", response_model=ServerInfoResponse)
async def get_server_info() -> ServerInfoResponse:
    """返回 web server 进程的项目根绝对路径。"""
    return ServerInfoResponse(repo_root=str(_REPO_ROOT), schema_version=1)


__all__ = ["router"]
