"""静态文件服务 + SPA fallback（v0.1.5）。

行为：

- ``GET /assets/*`` → ``web/dist/assets``（vite 打包产物）
- ``GET /{path}``    → 任何非 ``/api/`` / ``/ws/`` / ``/assets/`` 的路径都
  返回 ``web/dist/index.html``（SPA history mode）

dist 缺失时：

- ``cfg.web.dev_mode = True``  → 返回占位 JSON（``{"message": ..., "dev_mode": true}``）
  让 vite dev server 在另一个端口跑前端，后端只负责 API + WS
- ``cfg.web.dev_mode = False`` → :func:`install_static` 抛 :class:`RuntimeError`
  （让 uvicorn 启动失败，提醒先 ``make web-build``）

注意：

- :func:`install_static` 必须在所有 router 注册 **之后** 调用 —— ``/{path}``
  catch-all 会抢路径。
- ``StaticFiles(html=True)`` 会让 ``/assets/foo.js`` 直接命中文件；这里我们
  不在 mount 层启 html，单独写 fallback handler 控制 ``/api`` / ``/ws`` 不被抢。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from config_loader.models import Config

logger = logging.getLogger(__name__)


DEFAULT_DIST_DIR = "web/dist"


def _resolve_dist_dir(cfg: Config) -> Path:
    """从 cfg.web 读 dist 路径；未来可加 ``cfg.web.dist_dir`` 字段。

    v0.1.5 :class:`WebConfig` 没有 ``dist_dir`` 字段，按默认 ``web/dist``。
    """
    return Path(DEFAULT_DIST_DIR)


def install_static(app: FastAPI, cfg: Config) -> None:
    """注册静态文件路由 + SPA fallback。

    Raises:
        RuntimeError: ``cfg.web.dev_mode = False`` 但 dist 缺失。
    """
    dist_dir = _resolve_dist_dir(cfg)
    index = dist_dir / "index.html"
    assets_dir = dist_dir / "assets"

    if not index.is_file():
        if not cfg.web.dev_mode:
            raise RuntimeError(
                f"frontend dist not found at {index}; run `make web-build` or set web.dev_mode=true"
            )
        logger.warning(
            "frontend dist not found at %s; running in dev_mode (placeholder responses)",
            index,
        )
        _install_dev_placeholder(app)
        return

    # 生产路径：mount /assets + SPA fallback
    if assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(assets_dir)),
            name="assets",
        )
    else:
        logger.warning(
            "assets dir not found at %s; /assets/* will 404",
            assets_dir,
        )

    # dist 根目录下的静态文件（favicon 等）
    _dist_root_files = {f.name for f in dist_dir.iterdir() if f.is_file()}

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:  # type: ignore[unused-ignore]
        # 抢不到 /api /ws /assets：让前面注册的路由 / mount 先命中
        if full_path.startswith(("api/", "ws/", "assets/")):
            raise HTTPException(status_code=404)
        # dist 根目录的静态文件（如 favicon.png）直接返回
        if full_path in _dist_root_files:
            return FileResponse(str(dist_dir / full_path))
        return FileResponse(str(index))


def _install_dev_placeholder(app: FastAPI) -> None:
    """dev_mode + dist 缺失：占位 JSON。"""

    @app.get("/{full_path:path}")
    async def dev_placeholder(full_path: str) -> JSONResponse:  # type: ignore[unused-ignore]
        if full_path.startswith(("api/", "ws/")):
            raise HTTPException(status_code=404)
        return JSONResponse(
            content={
                "message": "frontend not built; run `make web-build` or vite dev",
                "dev_mode": True,
                "path": full_path,
            }
        )


__all__ = [
    "DEFAULT_DIST_DIR",
    "install_static",
]
