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
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
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
            try:
                from evolution.apply_executor import recover_pending_apply_jobs

                recovered_jobs = await recover_pending_apply_jobs(cfg)
                if recovered_jobs:
                    logger.info("Recovered %d evolution apply jobs", len(recovered_jobs))
            except Exception:
                logger.exception("evolution apply job recovery failed; continuing startup")
            progress.done()
            progress.cleanup()
            logger.info("ThreadManager started")

            try:
                from web.slash_candidates_loader import load_slash_candidates

                app.state.slash_candidates = await load_slash_candidates()
                logger.info("slash candidates loaded: %d items", len(app.state.slash_candidates))
            except Exception:
                logger.exception("slash candidates loading failed; continuing with empty list")
                app.state.slash_candidates = []
        except Exception:
            progress.fail("ThreadManager.start() failed")
            logger.exception("ThreadManager.start() failed; aborting app startup")
            raise

        # v0.2 cron ticker：scheduler.enabled 时起后台循环；多 worker 部署
        # 时每个进程都起 ticker，由 ``.scheduler.lock`` 文件锁保证只有一个
        # 抢到 due reservation。home 解析优先用 cfg.scheduler.home，缺省走
        # ``home / cron``（注意是 create_app 传入的 home_dir，**不是**全局
        # ``get_kongming_home()``，方便测试用 ``tmp_path`` 隔离）。
        ticker_task: asyncio.Task[None] | None = None
        ticker_stop: asyncio.Event | None = None
        ticker_runtime = None
        if cfg.scheduler.enabled:
            try:
                from scheduler.delivery import DeliveryDispatcher
                from scheduler.runtime_factory import build_cron_execution_bridge
                from scheduler.store import Store
                from scheduler.ticker import run_ticker_loop
                from web.cron_delivery import WebDeliverySink
                from web.cron_ws import get_broker

                cron_home = (
                    cfg.scheduler.home if cfg.scheduler.home is not None else (home / "cron")
                )
                scheduler_store = Store(cron_home)
                # v0.3 cron-delivery M4：lifespan ticker 主路径必须传 dispatcher，
                # 否则到点触发的 cron run 不走 WebDeliverySink → broker.broadcast，
                # 用户侧没有任何感知（修复 R2 round 1 P0-1）。
                # broker 走模块级单例（与 /ws/cron endpoint + run.py 装配的
                # WebDeliverySink 共享同一个实例）。
                from web.cron_delivery import ThreadTargetSink

                lifespan_cron_dispatcher = DeliveryDispatcher(
                    web_sink=WebDeliverySink(get_broker()),
                    target_sink=ThreadTargetSink(thread_manager),
                )
                preset_map = {p.id: p for p in cfg.web.llm_presets}
                ticker_runtime, ticker_bridge = build_cron_execution_bridge(
                    cfg,
                    scheduler_store,
                    event_sinks=[],
                    dispatcher=lifespan_cron_dispatcher,
                    preset_map=preset_map,
                    trace_dir=cron_home / "traces",
                )
                ticker_stop = asyncio.Event()
                ticker_task = asyncio.create_task(
                    run_ticker_loop(
                        scheduler_store,
                        ticker_bridge,
                        ticker_stop,
                        interval=cfg.scheduler.interval,
                        max_inflight=cfg.scheduler.max_inflight,
                    )
                )
                # 让 schedule_tool / 路由层能拿到同一份 store。
                app.state.scheduler_store = scheduler_store
                logger.info("scheduler ticker started (home=%s)", cron_home)
            except Exception:
                logger.exception("scheduler ticker startup failed; continuing without cron")
                ticker_task = None
                ticker_stop = None
                ticker_runtime = None

        try:
            yield
        finally:
            # 顺序：先停 ticker → 再关 thread_manager。两步都用 try/except
            # 吞，shutdown 任意阶段挂掉都不阻断剩余清理。
            if ticker_task is not None and ticker_stop is not None:
                ticker_stop.set()
                try:
                    await asyncio.wait_for(ticker_task, timeout=30.0)
                except (TimeoutError, asyncio.CancelledError):
                    ticker_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await ticker_task
                except Exception:
                    logger.exception("scheduler ticker raised during shutdown; ignoring")
                if ticker_runtime is not None:
                    aclose_fn = getattr(ticker_runtime, "aclose", None)
                    if aclose_fn is not None:
                        try:
                            await aclose_fn()
                        except Exception:
                            logger.exception("scheduler ticker runtime aclose failed; ignoring")

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
    from web._shared.session_manager import SessionManager as _SharedSessionManager
    from web.codex import CodexService

    app.state.config = cfg
    app.state.serializer = serializer
    app.state.password_hash = password_hash
    app.state.rate_limiter = rate_limiter
    app.state.thread_manager = thread_manager
    app.state.kongming_home = home
    app.state.claude_home = Path.home() / ".claude"
    app.state.workspace_root = Path.cwd()
    app.state.claude_session_manager = _SharedSessionManager()
    # codex 通道（与 claude_code 平级，独立 SessionManager 单例）
    app.state.codex_session_manager = _SharedSessionManager()
    app.state.codex_service = CodexService(
        app.state.codex_session_manager,
        thread_manager=thread_manager,
    )

    # 6. middleware（顺序：CSRF → Auth；先注册的最外层）
    app.add_middleware(AuthMiddleware, allow_docs=cfg.web.dev_mode)
    app.add_middleware(CSRFMiddleware)

    # 7. exception handler
    app.add_exception_handler(KongmingWebError, kongming_error_handler)

    # 8. routers
    from web.claude_code import router as claude_code_router
    from web.codex import router as codex_router
    from web.routers.auth import router as auth_router
    from web.routers.claude import router as claude_router
    from web.routers.codex import router as codex_rest_router
    from web.routers.config import router as config_router
    from web.routers.cron import router as cron_router
    from web.routers.diagrams import router as diagrams_router
    from web.routers.manage import router as manage_router
    from web.routers.presets import router as presets_router
    from web.routers.slash_candidates import router as slash_candidates_router
    from web.routers.threads import router as threads_router
    from web.routers.whiteboard import router as whiteboard_router
    from web.routers.workspace_git import router as workspace_git_router
    from web.routers.workspace_shell import router as workspace_shell_router

    app.include_router(auth_router)
    app.include_router(threads_router)
    app.include_router(presets_router)
    app.include_router(config_router)
    app.include_router(manage_router)
    app.include_router(whiteboard_router)
    app.include_router(diagrams_router)
    app.include_router(claude_code_router)
    app.include_router(codex_router)
    app.include_router(codex_rest_router)
    app.include_router(claude_router)
    app.include_router(workspace_git_router)
    app.include_router(workspace_shell_router)
    app.include_router(slash_candidates_router)
    app.include_router(cron_router)

    # 9. WS endpoint
    from web.cron_ws import register_cron_ws_routes
    from web.thread_status_ws import register_thread_status_routes
    from web.ws import register_ws_routes

    register_ws_routes(app)
    # v0.3 cron-delivery M4：cron 全局 WS 端点 /ws/cron。
    # broker 是模块级单例（web/cron_ws.py:get_broker），与 web/run.py 装配的
    # WebDeliverySink 共享同一个实例，无需通过 app.state 串引用。
    register_cron_ws_routes(app)
    register_thread_status_routes(app)

    # 10. static / SPA fallback（catch-all，必须最后注册）
    from web.static import install_static

    install_static(app, cfg)

    return app


__all__ = [
    "DEFAULT_LIFESPAN_SHUTDOWN_TIMEOUT",
    "create_app",
]
