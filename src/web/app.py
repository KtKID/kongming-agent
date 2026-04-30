"""FastAPI app factory（v0.1.5 web 宿主壳）。

:func:`create_app` 是装配入口：

1. 加载 session secret + password hash → 构造 itsdangerous serializer
2. 注册中间件（顺序：CSRF → Auth → router）
3. 注册 routers（auth / threads / presets / manage）
4. 注册 WS endpoint（``/ws/threads/{thread_id}``）
5. 注册 exception handlers（``KongmingWebError`` → ``ErrorResponseDTO``）
6. mount 静态服务（``/assets`` + SPA fallback）
7. 注册 lifespan：startup 调 ``thread_manager.start()``；shutdown 5s 超时调
   ``thread_manager.aclose_all()``

依赖注入约定：

- ``thread_manager`` 显式传入（不在 lifespan 里 new），便于测试 mock
- ``serializer`` / ``password_hash`` / ``rate_limiter`` / ``thread_manager`` 都挂
  ``app.state.*``，路由层通过 ``request.app.state.*`` 访问

import 边界：

- 本文件可 import ``web.*`` 子模块、``config_loader``、外部 fastapi
- **不可** import ``core`` / ``tools`` / ``executors`` / ``safety`` / ``host`` /
  ``cli`` / ``context`` / ``observability`` / ``memory`` / ``prompts``
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from config_loader.paths import get_kongming_home
from web.auth import AuthMiddleware, CSRFMiddleware, make_serializer
from web.auth_secrets import (
    WebAuthNotConfiguredError as _SecretsAuthNotConfigured,
)
from web.auth_secrets import (
    load_or_init_password_hash,
    load_or_init_session_secret,
)
from web.errors import KongmingWebError, kongming_error_handler
from web.startup_progress import StartupProgress

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from config_loader.models import Config
    from web.rate_limit import LoginRateLimiter
    from web.types import ThreadManagerProtocol

logger = logging.getLogger(__name__)


DEFAULT_LIFESPAN_SHUTDOWN_TIMEOUT = 5.0
"""shutdown 时调 ``thread_manager.aclose_all()`` 的超时（秒）。"""


def create_app(
    cfg: Config,
    thread_manager: ThreadManagerProtocol,
    *,
    home_dir: Path | None = None,
    rate_limiter: LoginRateLimiter | None = None,
    lifespan_shutdown_timeout: float = DEFAULT_LIFESPAN_SHUTDOWN_TIMEOUT,
) -> FastAPI:
    """装配 FastAPI app。

    Args:
        cfg: 整体 :class:`Config`；本函数只读 ``cfg.web.*``。
        thread_manager: 已构造好的 :class:`ThreadManagerProtocol` 实例（生产
            代码传 :class:`web.thread_manager.ThreadManager`，测试可传 fake）。
        home_dir: ``.kongming/`` 根目录；为 None 时调
            :func:`config_loader.paths.get_kongming_home`。测试时建议显式
            传 ``tmp_path / ".kongming"`` 隔离。
        rate_limiter: 自定义限流器；为 None 时构造默认实例。
        lifespan_shutdown_timeout: shutdown 调 aclose_all 的超时秒。

    Returns:
        装配好的 :class:`FastAPI` 实例；调用方传给 uvicorn / TestClient。

    Raises:
        web.errors.WebAuthNotConfiguredError: password.hash 缺失且无 env。
    """
    home = home_dir if home_dir is not None else get_kongming_home()

    # 1. secret + password
    secret = load_or_init_session_secret(home)
    try:
        password_hash = load_or_init_password_hash(home)
    except _SecretsAuthNotConfigured as exc:
        # 翻译成 web.errors 同名异常（让上层 except 统一）
        from web.errors import WebAuthNotConfiguredError

        raise WebAuthNotConfiguredError(str(exc)) from exc

    serializer = make_serializer(secret)

    # 2. rate limiter
    if rate_limiter is None:
        from web.rate_limit import LoginRateLimiter

        rate_limiter = LoginRateLimiter()

    # 3. lifespan
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        progress = StartupProgress(home)
        # startup
        try:
            progress.report("lifespan")
            await thread_manager.start()
            progress.done()
            progress.cleanup()
            logger.info("ThreadManager started")
        except Exception:
            progress.fail("ThreadManager.start() failed")
            logger.exception("ThreadManager.start() failed; aborting app startup")
            raise

        try:
            yield
        finally:
            # shutdown：5s 内强制 aclose_all
            try:
                await asyncio.wait_for(
                    thread_manager.aclose_all(),
                    timeout=lifespan_shutdown_timeout,
                )
                logger.info("ThreadManager closed cleanly")
            except TimeoutError:
                logger.warning(
                    "ThreadManager.aclose_all() exceeded %.1fs timeout; proceeding",
                    lifespan_shutdown_timeout,
                )
            except Exception:
                logger.exception("ThreadManager.aclose_all() raised; ignoring during shutdown")

    # 4. FastAPI 实例
    docs_url = "/docs" if cfg.web.dev_mode else None
    redoc_url = "/redoc" if cfg.web.dev_mode else None
    openapi_url = "/openapi.json" if cfg.web.dev_mode else None
    app = FastAPI(
        title="kongming-agent web (v0.1.5)",
        version="0.1.5",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )

    # 5. app.state 注入
    app.state.config = cfg
    app.state.serializer = serializer
    app.state.password_hash = password_hash
    app.state.rate_limiter = rate_limiter
    app.state.thread_manager = thread_manager
    app.state.kongming_home = home

    # 6. middleware（顺序：CSRF → Auth；先注册的最外层）
    app.add_middleware(AuthMiddleware, allow_docs=cfg.web.dev_mode)
    app.add_middleware(CSRFMiddleware)

    # 7. exception handler
    app.add_exception_handler(KongmingWebError, kongming_error_handler)

    # 8. routers
    from web.routers.auth import router as auth_router
    from web.routers.manage import router as manage_router
    from web.routers.presets import router as presets_router
    from web.routers.threads import router as threads_router

    app.include_router(auth_router)
    app.include_router(threads_router)
    app.include_router(presets_router)
    app.include_router(manage_router)

    # 9. WS endpoint
    from web.ws import register_ws_routes

    register_ws_routes(app)

    # 10. static / SPA fallback（catch-all，必须最后注册）
    from web.static import install_static

    install_static(app, cfg)

    return app


__all__ = [
    "DEFAULT_LIFESPAN_SHUTDOWN_TIMEOUT",
    "create_app",
]
