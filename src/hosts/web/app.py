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

- 本文件可 import ``web.*`` 子模块、``infrastructure.config``、外部 fastapi
- **不可** import ``core`` / ``tools`` / ``executors`` / ``safety`` / ``host`` /
  ``cli`` / ``prompting`` / ``infrastructure.tracing`` / ``memory`` / ``prompts``
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI

from hosts.web.app_support.startup_progress import StartupProgress
from hosts.web.auth.middleware import AuthMiddleware, CSRFMiddleware, make_serializer
from hosts.web.auth.secrets import (
    WebAuthNotConfiguredError as _SecretsAuthNotConfigured,
)
from hosts.web.auth.secrets import (
    load_or_init_password_hash,
    load_or_init_session_secret,
)
from hosts.web.errors import KongmingWebError, kongming_error_handler
from infrastructure.config.paths import get_kongming_home, resolve_kongming_path

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path
    from typing import Any

    from hosts.web.rate_limit import LoginRateLimiter
    from hosts.web.threads.types import ThreadManagerProtocol
    from infrastructure.config.models import Config
    from scheduler.store import Store

    SchedulerRuntimeFactory = Callable[[Store], tuple[Any, Any]]

logger = logging.getLogger(__name__)


DEFAULT_LIFESPAN_SHUTDOWN_TIMEOUT = 5.0
"""shutdown 时调 ``thread_manager.aclose_all()`` 的超时（秒）。"""


# src/hosts/web/app.py → parents[2] 指向项目根 (kongming-agent/)，
# 与 ``web.ctl._REPO_ROOT`` (parents[2]) 同源。这里独立计算以避免引入
# ``ctl.py`` 的 ``load_dotenv`` 启动副作用（同 server_info 模式）。
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _bootstrap_projects_registry(home: Path, repo_root: str) -> None:
    """Web server 启动时调用，登记当前 worktree + 迁移既有 thread metadata。

    幂等：每次启动都跑一遍，但 ``bootstrap_register_self`` /
    ``add_project`` 内部对已存在 cwd 不重复写盘。

    每个 registry 的 bootstrap 与 migrate 步骤互相独立 try/except，
    任一失败仅 log warning 不阻塞 web server 启动；下一次启动会重试。

    Args:
        home: ``.kongming/`` 根目录（由 :func:`infrastructure.config.paths.get_kongming_home`
            或 ``create_app(home_dir=...)`` 注入）。
        repo_root: 当前 web server 进程对应的项目根绝对路径。
    """
    from hosts.web.integrations.claude_code.projects_registry import (
        bootstrap_register_self as bootstrap_claude,
    )
    from hosts.web.integrations.claude_code.projects_registry import (
        migrate_from_thread_metadata as migrate_claude,
    )
    from hosts.web.integrations.codex.projects_registry import (
        bootstrap_register_self as bootstrap_codex,
    )
    from hosts.web.integrations.codex.projects_registry import (
        migrate_from_thread_metadata as migrate_codex,
    )

    registries: tuple[tuple[str, Callable[[Path, str], None], Callable[[Path], int]], ...] = (
        ("claude_code", bootstrap_claude, migrate_claude),
        ("codex", bootstrap_codex, migrate_codex),
    )
    migrated_counts: dict[str, int] = {}
    for kind, bootstrap_fn, migrate_fn in registries:
        try:
            bootstrap_fn(home, repo_root)
        except Exception:
            logger.warning("bootstrap %s registry failed", kind, exc_info=True)
        try:
            migrated_counts[kind] = migrate_fn(home)
        except Exception:
            logger.warning(
                "migrate %s registry from thread metadata failed",
                kind,
                exc_info=True,
            )
            migrated_counts[kind] = 0
    logger.info(
        "projects registry bootstrap: claude_code migrated=%d, codex migrated=%d",
        migrated_counts.get("claude_code", 0),
        migrated_counts.get("codex", 0),
    )


def create_app(
    cfg: Config,
    thread_manager: ThreadManagerProtocol,
    *,
    home_dir: Path | None = None,
    rate_limiter: LoginRateLimiter | None = None,
    scheduler_runtime_factory: SchedulerRuntimeFactory | None = None,
    task_progress_manager: object | None = None,
    lifespan_shutdown_timeout: float = DEFAULT_LIFESPAN_SHUTDOWN_TIMEOUT,
) -> FastAPI:
    """装配 FastAPI app。

    Args:
        cfg: 整体 :class:`Config`；本函数只读 ``cfg.web.*``。
        thread_manager: 已构造好的 :class:`ThreadManagerProtocol` 实例（生产
            代码传 :class:`web.threads.manager.ThreadManager`，测试可传 fake）。
        home_dir: ``.kongming/`` 根目录；为 None 时调
            :func:`infrastructure.config.paths.get_kongming_home`。测试时建议显式
            传 ``tmp_path / ".kongming"`` 隔离。
        rate_limiter: 自定义限流器；为 None 时构造默认实例。
        task_progress_manager: 当前 thread 任务进度服务；生产入口由 run.py 注入，
            测试可传 fake 或真实 Manager。
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
        password_hash = load_or_init_password_hash(
            home,
            initial_password=cfg.web.initial_password,
        )
    except _SecretsAuthNotConfigured as exc:
        # 翻译成 web.errors 同名异常（让上层 except 统一）
        from hosts.web.errors import WebAuthNotConfiguredError

        raise WebAuthNotConfiguredError(str(exc)) from exc

    serializer = make_serializer(secret)
    # 2. rate limiter
    if rate_limiter is None:
        from hosts.web.rate_limit import LoginRateLimiter

        rate_limiter = LoginRateLimiter()

    # 3. lifespan
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        progress = StartupProgress(home)
        # startup
        try:
            progress.report("lifespan")

            # full-log-v0.1 阶段 1：装配 FullLogger 单例（enabled=False 时是 no-op，
            # 不影响现有链路）。必须放在 thread_manager.start() 之前 —— ws_event_sink
            # 在 emit() 时 get_full_logger()，没 init 会拿到哑实例（数据丢但不抛）。
            try:
                from devtools import init_full_logger

                full_log_cfg = cfg.web.full_log.model_copy(
                    update={
                        "path": str(
                            resolve_kongming_path(
                                cfg.web.full_log.path,
                                kongming_home=home,
                            )
                        )
                    }
                )
                _full_logger = init_full_logger(full_log_cfg)
                await _full_logger.start()
                if _full_logger.enabled:
                    logger.info(
                        "FullLogger started: path=%s queue_size=%d",
                        _full_logger.path,
                        cfg.web.full_log.queue_size,
                    )
            except Exception:
                logger.exception("FullLogger init failed; continuing without full_log")

            await thread_manager.start()
            try:
                from evolution.apply_executor import recover_pending_apply_jobs

                recovered_jobs = await recover_pending_apply_jobs(
                    cfg,
                    kongming_home=home,
                )
                if recovered_jobs:
                    logger.info("Recovered %d evolution apply jobs", len(recovered_jobs))
            except Exception:
                logger.exception("evolution apply job recovery failed; continuing startup")

            # EvolutionManager 单例（频道无关）
            try:
                from evolution.evolution_manager import EvolutionManager

                _evolution_manager = EvolutionManager(config=cfg, kongming_home=home)
                app.state.evolution_manager = _evolution_manager
            except Exception:
                logger.exception("EvolutionManager init failed; continuing without evolution")
                _evolution_manager = None
                app.state.evolution_manager = None

            progress.done()
            progress.cleanup()
            logger.info("ThreadManager started")

            try:
                from hosts.web.app_support.slash_candidates_loader import load_slash_candidates

                app.state.slash_candidates = await load_slash_candidates()
                logger.info("slash candidates loaded: %d items", len(app.state.slash_candidates))
            except Exception:
                logger.exception("slash candidates loading failed; continuing with empty list")
                app.state.slash_candidates = []

            # web-projects-registry-v0.1 #8：把当前 worktree 登记进 claude_code /
            # codex 两个 registry，并迁移既有 thread metadata。文件 IO 用
            # ``asyncio.to_thread`` 包一层，避免阻塞 event loop（虽然只是几次
            # JSON 读写，但 lifespan 期间整个 web server 都在等）。
            try:
                await asyncio.to_thread(
                    _bootstrap_projects_registry,
                    home,
                    str(_REPO_ROOT),
                )
            except Exception:
                logger.exception("projects registry bootstrap failed; continuing without it")
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
                from hosts.web.app_support.cron_delivery import WebDeliverySink
                from hosts.web.websocket.cron import get_broker
                from scheduler.delivery import DeliveryDispatcher
                from scheduler.runtime_factory import build_cron_execution_bridge
                from scheduler.store import Store
                from scheduler.ticker import run_ticker_loop

                cron_home = (
                    resolve_kongming_path(cfg.scheduler.home, kongming_home=home)
                    if cfg.scheduler.home is not None
                    else (home / "cron")
                )
                scheduler_store = Store(cron_home)
                # v0.3 cron-delivery M4：lifespan ticker 主路径必须传 dispatcher，
                # 否则到点触发的 cron run 不走 WebDeliverySink → broker.broadcast，
                # 用户侧没有任何感知（修复 R2 round 1 P0-1）。
                # broker 走模块级单例（与 /ws/cron endpoint + run.py 装配的
                # WebDeliverySink 共享同一个实例）。
                from hosts.web.app_support.cron_delivery import ThreadTargetSink

                lifespan_cron_dispatcher = DeliveryDispatcher(
                    web_sink=WebDeliverySink(get_broker()),
                    target_sink=ThreadTargetSink(thread_manager),
                )
                preset_map = {p.id: p for p in cfg.web.llm_presets}
                if scheduler_runtime_factory is None:
                    ticker_runtime, ticker_bridge = build_cron_execution_bridge(
                        cfg,
                        scheduler_store,
                        event_sinks=[],
                        dispatcher=lifespan_cron_dispatcher,
                        preset_map=preset_map,
                        trace_dir=cron_home / "traces",
                    )
                else:
                    ticker_runtime, ticker_bridge = scheduler_runtime_factory(scheduler_store)
                    ticker_bridge._dispatcher = lifespan_cron_dispatcher
                    ticker_bridge._preset_map = dict(preset_map)
                    ticker_bridge._trace_dir = cron_home / "traces"
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

        # workflow dashboard scanner
        wf_scanner_task: asyncio.Task[None] | None = None
        wf_scanner_stop: asyncio.Event | None = None
        if cfg.workflow.enabled:
            try:
                from hosts.web.workflow.service import WorkflowService
                from hosts.web.workflow.store import WorkflowStore

                wf_home = (
                    resolve_kongming_path(cfg.workflow.home, kongming_home=home)
                    if cfg.workflow.home is not None
                    else (home / "workflows")
                )
                wf_store = WorkflowStore(wf_home)
                wf_service = WorkflowService(Path.cwd(), wf_store)
                app.state.workflow_service = wf_service
                app.state.workflow_scan_interval = cfg.workflow.scan_interval

                # 初始扫描
                wf_service.scan()

                async def _wf_scan_loop(
                    svc: WorkflowService, stop: asyncio.Event, interval: float
                ) -> None:
                    while not stop.is_set():
                        try:
                            await asyncio.sleep(interval)
                            if stop.is_set():
                                break
                            svc.scan()
                        except asyncio.CancelledError:
                            break
                        except Exception:
                            logger.exception("workflow scanner tick failed; will retry")

                wf_scanner_stop = asyncio.Event()
                wf_scanner_task = asyncio.create_task(
                    _wf_scan_loop(wf_service, wf_scanner_stop, cfg.workflow.scan_interval)
                )
                logger.info(
                    "workflow scanner started (home=%s, interval=%.0fs)",
                    wf_home,
                    cfg.workflow.scan_interval,
                )
            except Exception:
                logger.exception("workflow scanner startup failed; continuing without it")
                wf_scanner_task = None
                wf_scanner_stop = None

        try:
            yield
        finally:
            # workflow scanner shutdown
            if wf_scanner_task is not None and wf_scanner_stop is not None:
                wf_scanner_stop.set()
                try:
                    await asyncio.wait_for(wf_scanner_task, timeout=5.0)
                except (TimeoutError, asyncio.CancelledError):
                    wf_scanner_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await wf_scanner_task
                except Exception:
                    logger.exception("workflow scanner raised during shutdown; ignoring")
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

            # evolution manager shutdown
            if _evolution_manager is not None:
                try:
                    await _evolution_manager.aclose()
                except Exception:
                    logger.exception("EvolutionManager.aclose failed; ignoring during shutdown")

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

            # MCP / Web Search 共享 manager 挂在 runtime_factory 上；它的生命周期
            # 与 Web 进程一致，所有 thread runtime 关闭后统一释放 stdio 子进程。
            runtime_factory = getattr(app.state, "runtime_factory", None)
            mcp_runtime_registration = getattr(
                runtime_factory,
                "_mcp_runtime_registration",
                None,
            )
            if mcp_runtime_registration is not None:
                try:
                    await mcp_runtime_registration.aclose()
                except Exception:
                    logger.exception(
                        "McpRuntimeRegistrationManager.aclose failed; ignoring during shutdown"
                    )

            # full-log-v0.1 阶段 1：放在最后 flush 队列，让前面所有 shutdown 阶段
            # 产生的最后几帧也能落盘。未 init / 已 closed 时 aclose() 是 no-op。
            try:
                from devtools import get_full_logger

                await get_full_logger().aclose()
            except Exception:
                logger.exception("FullLogger.aclose failed; ignoring during shutdown")

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
    from hosts.web.integrations.codex import CodexService
    from hosts.web.shared.session_manager import SessionManager as _SharedSessionManager
    from hosts.web.whiteboard.manager import WhiteboardManager

    app.state.config = cfg
    app.state.serializer = serializer
    app.state.password_hash = password_hash
    app.state.rate_limiter = rate_limiter
    app.state.thread_manager = thread_manager
    app.state.task_progress_manager = task_progress_manager
    app.state.kongming_home = home
    app.state.claude_home = Path.home() / ".claude"
    app.state.workspace_root = Path.cwd()
    app.state.whiteboard_manager = WhiteboardManager(whiteboard_root=home / "whiteboard")
    app.state.claude_session_manager = _SharedSessionManager()

    # manage-config-tab #6：ConfigManager 单例（操作 setting.yaml 的唯一入口）。
    # yaml_path 优先用 KONGMING_CONFIG env；未显式设置时优先绑定当前
    # kongming_home 下的用户级 setting.yaml，再回落到 repo_root/config/setting.yaml。
    import os as _os

    from infrastructure.config import ConfigManager
    from infrastructure.config.paths import find_existing_kongming_home_config

    _home_config_path = find_existing_kongming_home_config(home)
    _config_yaml_path = Path(
        _os.environ.get(
            "KONGMING_CONFIG",
            str(_home_config_path or (_REPO_ROOT / "config" / "setting.yaml")),
        ),
    )
    app.state.config_manager = ConfigManager(
        yaml_path=_config_yaml_path,
        env_path=home / ".env",
    )
    app.state.config_restart_repo_root = _REPO_ROOT

    # full-log-v0.2 log viewer：LogSourceRegistry + LogReadService 单例。
    # registry 持静态 source 目录 + resolve 函数；service 读文件尾部 + 过滤。
    from hosts.web.dashboard.logs.registry import LogSourceRegistry
    from hosts.web.dashboard.logs.service import LogReadService

    app.state.log_source_registry = LogSourceRegistry(cfg, home)
    app.state.log_read_service = LogReadService(app.state.log_source_registry)

    # smart-approval-v1：Web 只调用装配 Manager，真实 policy/config/rules 由
    # safety.auto_approval.AutoApprovalManager 维护。
    from hosts.web.app_support.auto_approval_manager import WebAutoApprovalManager

    WebAutoApprovalManager.build(home).attach_to_app_state(app)

    # smart-approval-v2-inbox：全局审批 inbox broadcaster（per-process 单例）
    # 复用 /ws/thread-status 端点 fan-out approval.inbox.* 帧；
    # 维护 pending snapshot dict（重连补包用）+ bridge_registry（路由 resolve）
    from hosts.web.approvals.global_inbox import get_inbox_broadcaster

    app.state.approval_inbox_broadcaster = get_inbox_broadcaster()

    # media uploads（claude-image-paste-e2e §2）：注入 storage / registry /
    # validator 单例到 app.state，让 routers/uploads.py 直接消费而不是回落
    # 到 module-level lazy singleton。这样测试时可通过 override app.state 注入
    # 假实现。
    #
    # 注意：必须在 codex_service / claude_service 创建之**前**就绪，
    # 让通道 service 构造时能注入 asset_storage（codex-channel-image-paste §3）。
    from hosts.web.uploads.registry import AssetRegistry
    from hosts.web.uploads.storage import AssetStorage
    from hosts.web.uploads.validation import MediaUploadValidator

    app.state.asset_storage = AssetStorage(base_dir=home / "web" / "uploads")
    app.state.asset_registry = AssetRegistry()
    app.state.upload_validator = MediaUploadValidator(thread_manager)

    # network-layer-claude-keepalive-v0.1: 进程级单例，注入心跳配置
    # （来自 cfg.web.ws_heartbeat_* 真源；所有频道公用一个倒计时配置）。
    # 当前 Claude / generic 两类 per-thread channel 已接入 NetworkManager。
    # 注：get_kongming_home 走 infrastructure.config.paths 子模块直 import，避免
    # infrastructure.config/__init__.py → infrastructure.config.errors → core.errors 间接链
    # 触发 Contract 6 (web-app-shell-no-cross-pillar) 违规。
    from network import HeartbeatConfig, get_network_manager
    from network.manager import configure_heartbeat_log

    _network_manager = get_network_manager()
    _network_manager.configure(
        HeartbeatConfig(
            interval_ms=cfg.web.ws_heartbeat_interval_ms,
            timeout_ms=cfg.web.ws_heartbeat_timeout_ms,
            max_missed=cfg.web.ws_heartbeat_max_missed,
        ),
    )
    # 心跳诊断日志：写 <kongming_home>/logs/heartbeat/heartbeat.log
    # （旁路设计；删除本调用 + 重启即关闭日志，不影响功能）
    configure_heartbeat_log(home / "logs" / "heartbeat")
    from hosts.web.generic_channel_log import configure_generic_channel_log

    configure_generic_channel_log(home / "logs" / "generic-channel")
    app.state.network_manager = _network_manager

    # XSpace mobile pairing：SQLite 状态和 token 服务统一落到 <kongming_home>/web。
    from hosts.web.xspace_mobile import (
        LoginQrAuthService,
        LoginQrManager,
        MobileDeviceTokenService,
        MobilePairingManager,
        MobilePairingRepository,
    )

    app.state.xspace_mobile_pairing_repository = MobilePairingRepository(
        home / "web" / "mobile_pairing.db",
    )
    app.state.xspace_mobile_token_service = MobileDeviceTokenService(
        app.state.xspace_mobile_pairing_repository,
    )
    app.state.xspace_mobile_pairing_manager = MobilePairingManager(
        app.state.xspace_mobile_pairing_repository,
        app.state.xspace_mobile_token_service,
    )
    app.state.xspace_mobile_login_qr_manager = LoginQrManager(
        app.state.xspace_mobile_pairing_repository,
        app.state.xspace_mobile_token_service,
    )
    app.state.xspace_mobile_login_qr_auth_service = LoginQrAuthService(
        manager=app.state.xspace_mobile_login_qr_manager,
        rate_limiter=rate_limiter,
    )

    # Avatar message registry：Kongming 侧只提供消息注册 / REST 消费 / ack 真源，
    # XSpace Avatar 负责形象、气泡和展示策略。
    from hosts.web.avatar import AvatarManager
    from hosts.web.avatar.assistant_manager import AvatarAssistantManager
    from hosts.web.avatar.repository import AvatarMessageRepository

    app.state.avatar_message_repository = AvatarMessageRepository(
        home / "web" / "avatar_messages.db",
    )
    app.state.avatar_manager = AvatarManager(
        app.state.avatar_message_repository,
        AvatarAssistantManager(thread_manager),
    )

    # codex 通道（与 claude_code 平级，独立 SessionManager 单例）
    # codex-channel-image-paste §3：service 构造时注入 asset_storage，让
    # CodexImageCliArgsBuilder 能反推 asset 物理路径生成 --image flag
    app.state.codex_session_manager = _SharedSessionManager()
    app.state.codex_service = CodexService(
        app.state.codex_session_manager,
        thread_manager=thread_manager,
        asset_storage=app.state.asset_storage,
    )

    # 6. middleware（顺序：CSRF → Auth；先注册的最外层）
    app.add_middleware(AuthMiddleware, allow_docs=cfg.web.dev_mode)
    app.add_middleware(CSRFMiddleware)

    # 7. exception handler
    app.add_exception_handler(KongmingWebError, kongming_error_handler)

    # 8. routers
    from hosts.web.dashboard.config import router as dashboard_config_router
    from hosts.web.dashboard.logs.router import router as logs_router
    from hosts.web.integrations.claude_code import router as claude_code_router
    from hosts.web.integrations.codex import router as codex_router
    from hosts.web.routers.agent_workflows import router as agent_workflows_router
    from hosts.web.routers.auth import router as auth_router
    from hosts.web.routers.avatar import router as avatar_router
    from hosts.web.routers.claude import router as claude_router
    from hosts.web.routers.codex import router as codex_rest_router
    from hosts.web.routers.config import router as config_router
    from hosts.web.routers.cron import router as cron_router
    from hosts.web.routers.diagrams import router as diagrams_router
    from hosts.web.routers.health import router as health_router
    from hosts.web.routers.login_qr import router as login_qr_router
    from hosts.web.routers.manage import router as manage_router
    from hosts.web.routers.model_providers import router as model_providers_router
    from hosts.web.routers.presets import router as presets_router
    from hosts.web.routers.server_info import router as server_info_router
    from hosts.web.routers.sitian import router as sitian_router
    from hosts.web.routers.slash_candidates import router as slash_candidates_router
    from hosts.web.routers.thread_task_progress import router as thread_task_progress_router
    from hosts.web.routers.threads import router as threads_router
    from hosts.web.routers.uploads import router as uploads_router
    from hosts.web.routers.whiteboard import router as whiteboard_router
    from hosts.web.routers.workspace_git import router as workspace_git_router
    from hosts.web.routers.workspace_shell import router as workspace_shell_router
    from hosts.web.routers.xspace_mobile import router as xspace_mobile_router
    from hosts.web.routers.xspace_runtime import router as xspace_runtime_router

    app.include_router(auth_router)
    app.include_router(avatar_router)
    app.include_router(threads_router)
    app.include_router(thread_task_progress_router)
    app.include_router(agent_workflows_router)
    app.include_router(presets_router)
    app.include_router(model_providers_router)
    app.include_router(config_router)
    app.include_router(manage_router)
    # manage-config-tab #6：挂 /api/manage/config/* 5 个端点
    app.include_router(dashboard_config_router)
    # full-log-v0.2：挂 /api/manage/logs/* 2 个端点
    app.include_router(logs_router)
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
    app.include_router(sitian_router)
    app.include_router(server_info_router)
    app.include_router(uploads_router)
    app.include_router(health_router)
    app.include_router(login_qr_router)
    app.include_router(xspace_mobile_router)
    app.include_router(xspace_runtime_router)

    # workflow dashboard
    if cfg.workflow.enabled:
        from hosts.web.workflow.router import router as workflow_router

        app.include_router(workflow_router)

    # 9. WS endpoint
    from hosts.web.avatar.channel_manager import register_avatar_channel_routes
    from hosts.web.websocket.cron import register_cron_ws_routes
    from hosts.web.websocket.routes import register_ws_routes
    from hosts.web.websocket.thread_status import register_thread_status_routes

    register_avatar_channel_routes(app)
    register_ws_routes(app)
    # v0.3 cron-delivery M4：cron 全局 WS 端点 /ws/cron。
    # broker 是模块级单例（web/websocket/cron.py:get_broker），与 web/run.py 装配的
    # WebDeliverySink 共享同一个实例，无需通过 app.state 串引用。
    register_cron_ws_routes(app)
    register_thread_status_routes(app)

    # 10. static / SPA fallback（catch-all，必须最后注册）
    from hosts.web.static import install_static

    install_static(app, cfg)

    return app


__all__ = [
    "DEFAULT_LIFESPAN_SHUTDOWN_TIMEOUT",
    "create_app",
]
