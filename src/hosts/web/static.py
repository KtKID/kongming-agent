"""静态文件服务 + SPA fallback（v0.1.5）。

行为：

- ``GET /assets/*`` → ``web/dist/assets``（vite 打包产物）
- ``GET /brand/*``  → ``web/dist/brand``（public 静态资源）
- ``GET /{path}``    → 任何非静态前缀与非 API/WS 的路径都返回
  ``web/dist/index.html``（SPA history mode）

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
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from infrastructure.config.models import Config

logger = logging.getLogger(__name__)


DEFAULT_DIST_DIR = "web/dist"


def _packaged_dist_candidates() -> list[Path]:
    """返回包内 dist 候选路径。

    Returns:
        按优先级排列的包内 dist 目录候选。PyInstaller onefile 启动时
        ``sys._MEIPASS`` 指向临时解包目录。
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return []
    root = Path(str(base))
    return [
        root / "web" / "dist",
        root / "kongming" / "web" / "dist",
    ]


def _resolve_dist_dir(cfg: Config) -> Path:
    """解析前端 dist 路径。

    优先级：
    1. ``KONGMING_WEB_DIST``（由 ``--dist-dir`` 或外部宿主注入）
    2. PyInstaller 解包目录里的包内资源
    3. 源码工作目录下的 ``web/dist``
    """
    env_dist = os.environ.get("KONGMING_WEB_DIST")
    if env_dist and env_dist.strip():
        return Path(env_dist).expanduser().resolve()

    for candidate in _packaged_dist_candidates():
        if (candidate / "index.html").is_file():
            return candidate

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

    # 生产路径：mount 顶层静态目录 + SPA fallback
    mounted_prefixes: set[str] = set()

    if assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(assets_dir)),
            name="assets",
        )
        mounted_prefixes.add("assets")
    else:
        logger.warning(
            "assets dir not found at %s; /assets/* will 404",
            assets_dir,
        )

    for child in dist_dir.iterdir():
        if not child.is_dir() or child.name in mounted_prefixes:
            continue
        app.mount(
            f"/{child.name}",
            StaticFiles(directory=str(child)),
            name=child.name,
        )
        mounted_prefixes.add(child.name)

    # dist 根目录下的静态文件（favicon 等）
    _dist_root_files = {f.name for f in dist_dir.iterdir() if f.is_file()}
    _static_prefixes = tuple(f"{prefix}/" for prefix in sorted(mounted_prefixes))

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:  # type: ignore[unused-ignore]
        # 抢不到 /api /ws /静态目录：让前面注册的路由 / mount 先命中
        if full_path.startswith(("api/", "ws/", *_static_prefixes)):
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
