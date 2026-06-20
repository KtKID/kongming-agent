"""Entry point for ``python -m hosts.web.run``."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_WEB_HOST_ENVIRONMENT_ENV = "KONGMING_WEB_HOST_ENVIRONMENT"
WebHostEnvironment = Literal["browser", "xspace"]


@dataclass(frozen=True)
class WebRuntimeOptions:
    """Web sidecar 的运行时参数。

    Args:
        host: uvicorn 绑定地址。
        port: uvicorn 绑定端口；允许 ``0``，由 OS 分配空闲端口。
        home: kongming 运行时数据目录。
        config_path: 配置文件路径。
        dist_dir: 前端 dist 目录；为 ``None`` 时走环境变量或默认路径。
        server_origin: 手机等外部客户端访问 Web 的服务器 origin。
        public_origin: 兼容旧字段，语义同 server_origin。
        host_environment: Web sidecar 启动宿主环境。
        print_ready_json: 是否在启动成功后向 stdout 输出一次 ready JSON。
    """

    host: str
    port: int
    home: Path
    config_path: Path
    dist_dir: Path | None
    server_origin: str | None
    public_origin: str | None
    host_environment: WebHostEnvironment
    print_ready_json: bool


def _build_arg_parser() -> argparse.ArgumentParser:
    """构造 ``kongming-web`` 参数解析器。

    Returns:
        已配置运行时参数的 :class:`argparse.ArgumentParser`。
    """
    parser = argparse.ArgumentParser(prog="kongming-web")
    parser.add_argument("--host", help="Web server bind host.")
    parser.add_argument(
        "--port",
        type=int,
        help="Web server bind port. Use 0 to ask the OS for a free port.",
    )
    parser.add_argument("--home", type=Path, help="Kongming runtime home directory.")
    parser.add_argument("--config", type=Path, help="Explicit config file path.")
    parser.add_argument("--dist-dir", type=Path, help="Frontend dist directory.")
    parser.add_argument(
        "--public-origin",
        help="Compatibility alias for --server-origin.",
    )
    parser.add_argument(
        "--server-origin",
        help="Server origin used by mobile login QR, pairing QR, copy, and handoff URLs.",
    )
    parser.add_argument(
        "--host-environment",
        choices=("browser", "xspace"),
        help="Client runtime host environment exposed to the Web frontend.",
    )
    parser.add_argument(
        "--print-ready-json",
        action="store_true",
        help="Print one ready JSON line to stdout after the server starts.",
    )
    # 兼容早期契约名；语义与 --print-ready-json 完全一致。
    parser.add_argument(
        "--once-ready-json",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def _resolve_home(explicit_home: Path | None) -> Path:
    """解析运行时 home，并同步写回 ``KONGMING_HOME``。

    Args:
        explicit_home: CLI 传入的 home；为 ``None`` 时读取环境变量或默认值。

    Returns:
        绝对路径形式的 home。
    """
    if explicit_home is not None:
        home = explicit_home.expanduser().resolve()
        os.environ["KONGMING_HOME"] = str(home)
        return home

    from infrastructure.config.paths import get_kongming_home

    return get_kongming_home()


def _resolve_config_path(home: Path, explicit_config: Path | None) -> Path:
    """解析配置文件路径。

    Args:
        home: 已解析的 kongming home。
        explicit_config: CLI 显式配置路径。

    Returns:
        实际要传入 ``load_config`` 的路径。
    """
    if explicit_config is not None:
        config_path = explicit_config.expanduser().resolve()
        os.environ["KONGMING_CONFIG"] = str(config_path)
        return config_path

    env_config = os.environ.get("KONGMING_CONFIG")
    if env_config and env_config.strip():
        return Path(env_config).expanduser().resolve()

    from infrastructure.config.paths import find_existing_kongming_home_config

    existing_home_config = find_existing_kongming_home_config(home)
    if existing_home_config is not None:
        os.environ["KONGMING_CONFIG"] = str(existing_home_config)
        return existing_home_config

    return Path("config/setting.yaml")


def _resolve_dist_dir(explicit_dist_dir: Path | None) -> Path | None:
    """解析前端 dist 参数，并通过 ``KONGMING_WEB_DIST`` 交给 static 模块。

    Args:
        explicit_dist_dir: CLI 显式 dist 路径。

    Returns:
        绝对路径形式的 dist 目录；未显式指定时返回环境变量或 ``None``。
    """
    if explicit_dist_dir is not None:
        dist_dir = explicit_dist_dir.expanduser().resolve()
        os.environ["KONGMING_WEB_DIST"] = str(dist_dir)
        return dist_dir

    env_dist = os.environ.get("KONGMING_WEB_DIST")
    if env_dist and env_dist.strip():
        return Path(env_dist).expanduser().resolve()
    return None


def _normalize_server_origin(value: str | None) -> str | None:
    """归一化外部客户端访问 Web 的服务器 origin。

    Args:
        value: CLI / env / config 提供的 origin 字符串。

    Returns:
        标准 ``scheme://host[:port]`` origin；空值返回 ``None``。
    """
    if value is None:
        return None
    origin = value.strip().rstrip("/")
    if not origin:
        return None
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("web server origin must be an http(s) origin")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("web server origin must not include path, query, or fragment")
    return f"{parsed.scheme}://{parsed.netloc}"


def _normalize_host_environment(value: str | None) -> WebHostEnvironment | None:
    """归一化 Web sidecar 宿主环境。"""
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in {"browser", "xspace"}:
        raise ValueError("web host environment must be 'browser' or 'xspace'")
    if normalized == "xspace":
        return "xspace"
    return "browser"


def _resolve_runtime_options(argv: list[str] | None = None) -> WebRuntimeOptions:
    """解析 Web sidecar 运行时参数。

    Args:
        argv: 参数列表；为 ``None`` 时读取 ``sys.argv``。

    Returns:
        归一化后的运行时参数。
    """
    args = _build_arg_parser().parse_args(argv)
    home = _resolve_home(args.home)
    dist_dir = _resolve_dist_dir(args.dist_dir)
    host = args.host or os.environ.get("KONGMING_WEB_HOST") or ""
    server_origin = _normalize_server_origin(
        args.server_origin
        or os.environ.get("KONGMING_WEB_SERVER_ORIGIN")
        or args.public_origin
        or os.environ.get("KONGMING_WEB_PUBLIC_ORIGIN")
    )
    host_environment = (
        _normalize_host_environment(
            args.host_environment or os.environ.get(_WEB_HOST_ENVIRONMENT_ENV)
        )
        or "browser"
    )
    os.environ[_WEB_HOST_ENVIRONMENT_ENV] = host_environment
    config_path = _resolve_config_path(home, args.config)
    raw_port = args.port
    if raw_port is None:
        env_port = os.environ.get("KONGMING_WEB_PORT")
        raw_port = int(env_port) if env_port and env_port.strip() else -1
    if raw_port < -1 or raw_port > 65535:
        raise ValueError(f"web port must be 0-65535, got {raw_port}")
    return WebRuntimeOptions(
        host=host,
        port=raw_port,
        home=home,
        config_path=config_path,
        dist_dir=dist_dir,
        server_origin=server_origin,
        public_origin=server_origin,
        host_environment=host_environment,
        print_ready_json=bool(args.print_ready_json or args.once_ready_json),
    )


def _build_uvicorn_log_config() -> dict[str, object]:
    """Return uvicorn logging config with timestamps for server.log."""
    fmt = "%(asctime)s %(levelname)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": fmt,
                "datefmt": datefmt,
            },
            "access": {
                "format": fmt,
                "datefmt": datefmt,
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
    }


def _override_web_bind_config(
    cfg: Any,
    *,
    host: str,
    port: int,
    server_origin: str | None = None,
    public_origin: str | None = None,
    host_environment: WebHostEnvironment | None = None,
) -> Any:
    """把运行时绑定地址写入 Config 副本。

    Args:
        cfg: ``load_config`` 返回的配置对象。
        host: 实际绑定 host。
        port: 实际绑定端口，必须大于 0。
        server_origin: 运行时指定的服务器 origin；``None`` 时保留配置文件值。
        public_origin: 兼容旧字段；``server_origin`` 为空时作为服务器 origin 使用。
        host_environment: 运行时指定的宿主环境；``None`` 时保留配置文件值。

    Returns:
        更新了 ``web.host`` / ``web.port`` 的 Config 副本。
    """
    updates: dict[str, object] = {"host": host, "port": port}
    origin = server_origin if server_origin is not None else public_origin
    if origin is not None:
        updates["server_origin"] = origin
        updates["public_origin"] = origin
    if host_environment is not None:
        updates["host_environment"] = host_environment
    web_cfg = cfg.web.model_copy(update=updates)
    return cfg.model_copy(update={"web": web_cfg})


def _load_config_with_runtime_overrides(
    config_path: Path,
    *,
    host_environment: WebHostEnvironment | None,
) -> Any:
    """读取配置，并让启动宿主环境作为进程级 env 覆盖先于校验生效。"""
    from infrastructure.config import load_config

    if host_environment is None:
        return load_config(config_path)

    os.environ[_WEB_HOST_ENVIRONMENT_ENV] = host_environment
    return load_config(config_path)


def _format_base_url(host: str, port: int) -> str:
    """格式化 loopback URL，兼容 IPv6 host。

    Args:
        host: 绑定 host。
        port: 绑定端口。

    Returns:
        HTTP base URL。
    """
    if ":" in host and not host.startswith("["):
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def _now_iso_for_timezone(timezone_name: str) -> str:
    """按配置时区生成当前时间 ISO 字符串。

    Args:
        timezone_name: IANA timezone name，例如 ``Asia/Shanghai``。

    Returns:
        带时区 offset 的 ISO 时间字符串。
    """
    try:
        tz: tzinfo = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning("Invalid scheduler.default_timezone=%r; falling back to UTC", timezone_name)
        tz = UTC
    return datetime.now(tz).isoformat()


def _bind_runtime_socket(host: str, port: int) -> tuple[socket.socket, str, int]:
    """预绑定 uvicorn socket，支持 ``port=0`` 的真实端口回填。

    Args:
        host: bind host。
        port: bind port，允许 0。

    Returns:
        ``(socket, bound_host, bound_port)``。
    """
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(
        host,
        port,
        type=socket.SOCK_STREAM,
    ):
        sock = socket.socket(family, socktype, proto)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
            sock.listen(2048)
            bound = sock.getsockname()
            return sock, str(bound[0]), int(bound[1])
        except OSError as exc:
            last_error = exc
            sock.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"failed to resolve bind address {host}:{port}")


def _build_ready_payload(
    *,
    host: str,
    port: int,
    home: Path,
    dist_dir: Path | None,
    server_origin: str | None = None,
    public_origin: str | None = None,
    timezone_name: str = "UTC",
) -> dict[str, object]:
    """构造 ready JSON / server.json payload。

    Args:
        host: 实际绑定 host。
        port: 实际绑定端口。
        home: kongming home。
        dist_dir: 前端 dist 目录。
        public_origin: 手机等外部客户端访问 Web 的公开 origin。
        timezone_name: 配置里的 IANA 时区名。
        server_origin: 手机等外部客户端访问 Web 的服务器 origin。
        public_origin: 兼容旧字段，语义同 server_origin。

    Returns:
        可 JSON 序列化的 ready payload。
    """
    base_url = _format_base_url(host, port)
    server_json = home / "web" / "server.json"
    return {
        "type": "kongming_web_ready",
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "base_url": base_url,
        "server_origin": server_origin or public_origin,
        "public_origin": public_origin or server_origin,
        "health_url": f"{base_url}/health",
        "home": str(home),
        "server_json": str(server_json),
        "dist_dir": str(dist_dir) if dist_dir is not None else None,
        "started_at": _now_iso_for_timezone(timezone_name),
    }


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    """原子写入 JSON 文件。

    Args:
        path: 目标 JSON 文件。
        payload: 要写入的 JSON payload。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def _write_ready_payload(
    *,
    host: str,
    port: int,
    home: Path,
    dist_dir: Path | None,
    server_origin: str | None = None,
    public_origin: str | None = None,
    timezone_name: str = "UTC",
    print_ready_json: bool,
) -> dict[str, object]:
    """写入 ``server.json``，并按需向 stdout 输出一次 ready JSON。

    Args:
        host: 实际绑定 host。
        port: 实际绑定端口。
        home: kongming home。
        dist_dir: 前端 dist 目录。
        public_origin: 手机等外部客户端访问 Web 的公开 origin。
        timezone_name: 配置里的 IANA 时区名。
        server_origin: 手机等外部客户端访问 Web 的服务器 origin。
        public_origin: 兼容旧字段，语义同 server_origin。
        print_ready_json: 是否输出 ready JSON。

    Returns:
        已写入的 ready payload。
    """
    payload = _build_ready_payload(
        host=host,
        port=port,
        home=home,
        dist_dir=dist_dir,
        server_origin=server_origin,
        public_origin=public_origin,
        timezone_name=timezone_name,
    )
    _write_json_atomic(home / "web" / "server.json", payload)
    if print_ready_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        sys.stdout.flush()
    return payload


def _write_startup_error(home: Path, message: str) -> None:
    """写入启动失败诊断文件。

    Args:
        home: kongming home。
        message: 错误消息。
    """
    payload = {
        "type": "kongming_web_startup_error",
        "pid": os.getpid(),
        "error": message,
        "started_at": datetime.now(UTC).isoformat(),
    }
    _write_json_atomic(home / "web" / "startup.json", payload)


async def _serve_with_ready_payload(
    *,
    uvicorn_module: Any,
    app: Any,
    runtime_socket: socket.socket,
    bound_host: str,
    bound_port: int,
    home: Path,
    dist_dir: Path | None,
    server_origin: str | None,
    public_origin: str | None,
    timezone_name: str,
    log_level: str,
    print_ready_json: bool,
) -> None:
    """启动 uvicorn，并在 startup 后写 ready payload。

    Args:
        uvicorn_module: 已 import 的 uvicorn 模块。
        app: FastAPI app 实例。
        runtime_socket: 已预绑定的 socket。
        bound_host: 实际绑定 host。
        bound_port: 实际绑定端口。
        home: kongming home。
        dist_dir: 前端 dist 目录。
        public_origin: 手机等外部客户端访问 Web 的公开 origin。
        timezone_name: 配置里的 IANA 时区名。
        server_origin: 手机等外部客户端访问 Web 的服务器 origin。
        public_origin: 兼容旧字段，语义同 server_origin。
        log_level: uvicorn 日志级别。
        print_ready_json: 是否向 stdout 打印 ready payload。
    """
    config = uvicorn_module.Config(
        app,
        host=bound_host,
        port=bound_port,
        log_level=log_level,
        log_config=_build_uvicorn_log_config(),
    )
    server = uvicorn_module.Server(config)
    sockets = [runtime_socket]
    with server.capture_signals():
        if not config.loaded:
            config.load()
        server.lifespan = config.lifespan_class(config)
        logging.getLogger("uvicorn.error").info("Started server process [%d]", os.getpid())
        await server.startup(sockets=sockets)
        if not server.should_exit:
            _write_ready_payload(
                host=bound_host,
                port=bound_port,
                home=home,
                dist_dir=dist_dir,
                server_origin=server_origin,
                public_origin=public_origin,
                timezone_name=timezone_name,
                print_ready_json=print_ready_json,
            )
            await server.main_loop()
        if server.started:
            await server.shutdown(sockets=sockets)
            logging.getLogger("uvicorn.error").info("Finished server process [%d]", os.getpid())


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    try:
        import uvicorn
    except ImportError as exc:
        sys.stderr.write(f"web dependencies not installed; run `uv sync --all-extras`: {exc}\n")
        return 1

    from hosts.web.app import create_app
    from hosts.web.app_support.app_lock import acquire_app_instance_lock, release_app_instance_lock
    from hosts.web.app_support.startup_progress import StartupProgress
    from hosts.web.threads.manager import ThreadManager

    try:
        options = _resolve_runtime_options(argv)
    except Exception as exc:
        sys.stderr.write(f"invalid web runtime options: {exc}\n")
        return 2

    home = options.home

    # P0 #1 修复（reports/cr/cr-report-20260519-web-crash-investigation.md）：
    # 启动时抢 web app 单实例锁。防止多个 web 进程同时跑 ticker loop 抢
    # scheduler file lock → SchedulerBusyError 死循环（5/18 实测 30 min
    # 内 server.log 涨 29 MB / 45 万行）。活进程冲突 → sys.exit(1)；孤儿
    # 锁（持锁 PID 已死）→ 自动清理重抢。
    app_lock_fd: int | None = acquire_app_instance_lock(home)

    try:
        progress = StartupProgress(home)
        progress.report("imports")

        cfg = _load_config_with_runtime_overrides(
            options.config_path,
            host_environment=options.host_environment,
        )
        progress.report("config")

        if not cfg.web.enabled:
            progress.fail("web.enabled is false")
            sys.stderr.write(
                "cfg.web.enabled=false; set web.enabled=true in config/setting.yaml "
                "or KONGMING_WEB_ENABLED=1 env to start web server\n"
            )
            return 1

        bind_host = options.host or cfg.web.host or "127.0.0.1"
        bind_port = options.port if options.port >= 0 else int(cfg.web.port)
        runtime_socket, actual_host, actual_port = _bind_runtime_socket(bind_host, bind_port)
        cfg = _override_web_bind_config(
            cfg,
            host=actual_host,
            port=actual_port,
            server_origin=options.server_origin,
            public_origin=options.public_origin,
            host_environment=options.host_environment,
        )

        runtime_factory = _make_runtime_factory(cfg)
        progress.report("factory")

        # claude-image-paste-e2e P1 #2:注入 AssetStorage 让 ThreadManager.delete_thread
        # 同步清理 thread 名下上传资产(images / videos / files),否则 thread 删除留孤儿磁盘。
        from hosts.web.uploads.storage import AssetStorage

        asset_storage = AssetStorage()

        tm = ThreadManager(
            cfg,
            kongming_home=home,
            runtime_factory=runtime_factory,  # type: ignore[arg-type]
            asset_storage=asset_storage,
        )

        try:
            from sessions import SessionTaskProgressManager

            app = create_app(
                cfg,
                tm,
                home_dir=home,
                scheduler_runtime_factory=getattr(
                    runtime_factory, "_scheduler_runtime_factory", None
                ),
                task_progress_manager=SessionTaskProgressManager.from_config(cfg),
            )
        except Exception as exc:
            runtime_socket.close()
            progress.fail(f"create_app failed: {exc}")
            _write_startup_error(home, str(exc))
            sys.stderr.write(f"create_app failed: {exc}\n")
            return 1
        # smart-approval-generic-chat-autoallow task #6：把 app 引用回挂给
        # runtime_factory，让 generic_chat 通道装配时（lazy / per-thread）能从
        # ``app.state.auto_approval_policy.config_store`` 取**同一份** ConfigStore
        # 注入 :class:`ApprovalRules`，实现 "用户 UI 一处 toggle 即时生效于所有通道"。
        # 此处 attr 设置而非 factory 入参修改：ThreadManager 调用签名
        # ``factory(thread_id, preset_id, adapter, sinks)`` 不可破坏。
        setattr(runtime_factory, "_app", app)  # noqa: B010
        app.state.runtime_factory = runtime_factory
        progress.report("app")

        log_level = cfg.logging.level.lower()
        progress.report("uvicorn")
        try:
            asyncio.run(
                _serve_with_ready_payload(
                    uvicorn_module=uvicorn,
                    app=app,
                    runtime_socket=runtime_socket,
                    bound_host=cfg.web.host,
                    bound_port=cfg.web.port,
                    home=home,
                    dist_dir=options.dist_dir,
                    server_origin=cfg.web.server_origin,
                    public_origin=cfg.web.public_origin,
                    timezone_name=cfg.scheduler.default_timezone,
                    log_level=log_level,
                    print_ready_json=options.print_ready_json,
                )
            )
        except Exception as exc:
            progress.fail(f"uvicorn.run failed: {exc}")
            _write_startup_error(home, str(exc))
            raise
        return 0
    except Exception as exc:
        _write_startup_error(home, str(exc))
        sys.stderr.write(f"web server startup failed: {exc}\n")
        return 1
    finally:
        # 显式释放锁（进程退出 OS 也会自动释放；本句是 best-effort 兜底）
        release_app_instance_lock(app_lock_fd)


def _build_manager_and_inbox_sink(*, app: Any) -> Any:
    """构造或获取 ApprovalManager 单例，幂等注入 InboxEventSink + AutoApprovalPolicy。

    approval-rules-unified（破坏性改造）：

    - 删除 ``default_timeout_ms`` 参数。manager 走默认 60s 即可（与
      :attr:`web.app_support.host_adapter.WebHostAdapter._timeout` 默认对齐）；
      ``cfg.web.pending_approval_timeout_seconds`` 仍归 host_adapter 用，
      不再串通到 manager / ApprovalRules。
    - :class:`ApprovalRules` 注入 ``app.state.auto_approval_policy`` 完整实例
      （而非仅 ``config_store``）——让 generic_chat 通道完全复用 claude_code
      的 24 条规则 + per-cwd 总开关 + audit 评估快照（同一份真源）。
    - policy 缺失（test 环境 / lifespan 漂移）→ ``ApprovalRules`` fail-closed
      走默认 ask + 60s（不抛错）；正常生产路径 policy 必就绪（由
      ``create_app`` lifespan 装配；见 ``src/hosts/web/app.py`` 真源）。

    阶段 1 (smart-approval-manager-v0.5)：generic_chat 通道默认走 manager 路径，
    无 feature flag；回滚 = git revert factory 内的 prompt_fn 装配。
    manager 是 per-process 单例（``get_approval_manager``）；
    InboxEventSink 用 sink 类型判定幂等，多 cell 装配只注入一次。

    Args:
        app: FastAPI app 实例（lazy；首次装配时 ``app.state.auto_approval_policy``
            必须已就绪，由 create_app lifespan 装配；缺失时 policy=None，
            fail-safe 降级到「所有 cwd 默认 ask + 60s」行为）。
    """
    from hosts.web.approvals.global_inbox import get_inbox_broadcaster
    from hosts.web.avatar import AvatarManager
    from hosts.web.avatar.approval_sink import AvatarApprovalSink
    from safety.approval.manager import get_approval_manager
    from safety.approval.rules import ApprovalRules
    from safety.inbox.event_sink import InboxEventSink

    broadcaster = get_inbox_broadcaster()
    avatar_manager = getattr(app.state, "avatar_manager", None) if app is not None else None
    policy = getattr(app.state, "auto_approval_policy", None) if app is not None else None
    manager = get_approval_manager(
        rules=ApprovalRules(policy=policy),
    )
    # 幂等：只在首次装配时注入 InboxEventSink；按 sink 类型判定
    has_inbox_sink = any(isinstance(s, InboxEventSink) for s in manager._event_sinks)
    if not has_inbox_sink:
        sink = InboxEventSink(broadcaster=broadcaster, manager=manager)
        manager.register_event_sink(sink)
    if isinstance(avatar_manager, AvatarManager) and not manager.has_event_sink_type(
        AvatarApprovalSink
    ):
        manager.register_event_sink(AvatarApprovalSink(avatar_manager))
    return manager


def _resolve_default_cwd_for_thread(app: Any, thread_id: str) -> str:
    """解析 thread 装配时的默认 cwd（thread-cwd-fallback 任务 #4）。

    优先级：

    1. 通过 ``app.state.thread_manager.list_threads()`` 反查 metadata，
       命中后走 :func:`web.workspace.model.resolve_workspace_cwd`：
       ``meta.cwd`` 非空直接返回，空时 fallback 到 server 启动目录
       （``app.state.workspace_root``）。
    2. ``app`` 未注入 / thread_manager 缺失 / list_threads 抛错 / metadata
       查不到 → **显式降级**到 ``app.state.workspace_root`` 字符串；
       连 workspace_root 也拿不到时退到当前进程 cwd。

    用户心智：未绑 cwd 的纯聊天 thread，审批默认走 server 启动目录的 cwd 配置
    （而不是空字符串导致命中不到任何 ProjectConfig）。

    Args:
        app: FastAPI app 实例；可能为 ``None``（main() 装配链尚未把 app 回挂到
            runtime_factory 时）。
        thread_id: 待装配 cell 对应的 thread id。

    Returns:
        非空字符串：thread.cwd / server cwd / 进程 cwd 三级兜底。
    """
    from hosts.web.workspace.model import resolve_workspace_cwd

    def _cwd_string(path: Path) -> str:
        return path.as_posix()

    server_workspace_root: Path
    if app is not None:
        server_workspace_root = Path(getattr(app.state, "workspace_root", Path.cwd()))
    else:
        server_workspace_root = Path.cwd()

    if app is None:
        return _cwd_string(server_workspace_root)

    tm = getattr(app.state, "thread_manager", None)
    if tm is None:
        return _cwd_string(server_workspace_root)

    try:
        metas = tm.list_threads()
    except Exception as exc:  # pragma: no cover - 防御性：扫盘异常不应阻断装配
        logger.warning(
            "thread_manager.list_threads failed during cwd resolve for %s: %s",
            thread_id,
            exc,
        )
        return _cwd_string(server_workspace_root)

    meta = next((m for m in metas if m.id == thread_id), None)
    if meta is None:
        # 装配链上 thread metadata 通常已写盘（先 create_thread → metadata.json
        # → 再调 factory）。拿不到属异常路径，显式降级到 server cwd 而非
        # silently 走空字符串。
        return _cwd_string(server_workspace_root)

    return resolve_workspace_cwd(meta, server_workspace_root)


def _make_runtime_factory(cfg: object) -> object:
    """Build a runtime factory for web thread cells and cron runs."""
    from hosts.shared.mcp_runtime_registration import McpRuntimeRegistrationManager
    from hosts.shared.session_bridge import SessionBridge
    from infrastructure.config.models import Config, LLMPresetConfig
    from infrastructure.config.paths import get_kongming_home
    from infrastructure.tracing import JsonlTraceSink
    from prompting.skills.skill_loader import format_skill_listing, load_skill_specs
    from runtime_assembly.native_runtime import NativeRuntime
    from safety.approval.manager import make_manager_prompt_fn
    from sessions import SessionBootstrap, build_session
    from tools import (
        ToolRegistry,
        build_default_approval,
        build_default_registry,
        register_choice_tool,
        register_evolution_write_tool_if_enabled,
        register_schedule_tool_if_enabled,
        register_task_progress_tool,
    )

    assert isinstance(cfg, Config)
    real_cfg: Config = cfg
    home = get_kongming_home()

    def _current_preset_map() -> dict[str, LLMPresetConfig]:
        return {p.id: p for p in real_cfg.web.llm_presets}

    _registry_cache: list[ToolRegistry | None] = [None]
    _enabled_tools_cache: list[list[str] | None] = [None]
    _agent_workflow_handle_cache: list[Any | None] = [None]
    _agent_role_manager_cache: list[Any | None] = [None]
    _instructions_cache: list[str | None] = [None]
    _origins_cache: list[list[str] | None] = [None]
    _workflow_prompt_cache_key: list[object | None] = [None]
    _scheduler_runtime_factory_cache: list[object | None] = [None]
    _mcp_runtime_registration_cache: list[McpRuntimeRegistrationManager | None] = [None]
    _cache_lock = asyncio.Lock()

    def _prompt_hash(text: str) -> str:
        return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"

    def _workflow_prompt_cache_matches(cache_key: object | None, candidate: object) -> bool:
        return (
            cache_key is not None
            and getattr(cache_key, "base_instructions_hash", None)
            == getattr(candidate, "base_instructions_hash", None)
            and getattr(cache_key, "workflow_template_version", None)
            == getattr(candidate, "workflow_template_version", None)
            and getattr(cache_key, "workflow_listing_hash", None)
            == getattr(candidate, "workflow_listing_hash", None)
        )

    async def _build_workflow_prompt_cache_candidate(
        *,
        workflow_render: Any,
        sitian_root: Path | None,
    ) -> tuple[object, list[Any]]:
        from application.agent_workflows.prompt_catalog import (
            build_workflow_prompt_cache_key,
        )
        from prompting.instructions.instruction_loader import (
            InstructionLoader,
            load_instruction_sources,
        )

        base_sources = await load_instruction_sources(
            kongming_home=home,
            sitian_root=sitian_root,
        )
        base_rendered = InstructionLoader.render(base_sources)
        return (
            build_workflow_prompt_cache_key(
                base_instructions_hash=_prompt_hash(base_rendered),
                render=workflow_render,
            ),
            base_sources,
        )

    def _insert_workflow_prompt_source(
        *,
        base_sources: list[Any],
        workflow_render: Any,
    ) -> list[Any]:
        from prompting.instructions.instruction_loader import InstructionSource

        if not getattr(workflow_render, "text", "").strip():
            return [*base_sources]
        workflow_source = InstructionSource(
            origin=workflow_render.origin,
            content=workflow_render.text,
        )
        insert_at = 1 if base_sources and base_sources[0].origin == "runtime" else 0
        return [*base_sources[:insert_at], workflow_source, *base_sources[insert_at:]]

    async def _ensure_shared_assets(sinks: object) -> None:
        from application.agent_workflows.prompt_catalog import (
            build_default_workflow_prompt_listing,
        )
        from prompting.instructions.instruction_loader import InstructionLoader

        sitian_raw = os.environ.get("SITIAN_PROMPT_ROOT", "").strip()
        sitian_root = Path(sitian_raw).expanduser().resolve() if sitian_raw else None

        async with _cache_lock:
            workflow_render = build_default_workflow_prompt_listing()
            workflow_cache_key, base_sources = await _build_workflow_prompt_cache_candidate(
                workflow_render=workflow_render,
                sitian_root=sitian_root,
            )
            if _instructions_cache[0] is not None and _workflow_prompt_cache_matches(
                _workflow_prompt_cache_key[0],
                workflow_cache_key,
            ):
                return

            sink_list = list(sinks) if sinks else []  # type: ignore[call-overload]
            skill_specs_list = await load_skill_specs(
                home,
                workspace=Path.cwd(),
                event_sinks=sink_list,
            )
            listing = format_skill_listing(skill_specs_list)
            sources = _insert_workflow_prompt_source(
                base_sources=base_sources,
                workflow_render=workflow_render,
            )
            rendered = InstructionLoader.render(sources)
            origins = [source.origin for source in sources]
            if listing:
                rendered = rendered + f"\n\n# skills\n{listing}"
                origins = [*origins, "skills"]

            skill_specs = {spec.name: spec for spec in skill_specs_list}
            registry = build_default_registry(
                file_enabled=real_cfg.tool.file.enabled,
                shell_enabled=real_cfg.tool.shell.enabled,
                shell_timeout_seconds=real_cfg.tool.shell.timeout_seconds,
                shell_max_stream_bytes=real_cfg.tool.shell.max_stream_bytes,
                shell_terminate_grace_seconds=real_cfg.tool.shell.terminate_grace_seconds,
                file_read_max_bytes=real_cfg.tool.file.read_max_bytes,
                skill_specs=skill_specs or None,
                skill_event_sinks=sink_list,
            )
            agent_role_manager, agent_workflow_handle = _register_agent_workflow_tools(
                registry,
                role_dir=home / "agent_roles",
            )

            app_ref = getattr(factory, "_app", None)
            thread_manager = getattr(getattr(app_ref, "state", None), "thread_manager", None)
            cron_dispatcher = None
            if real_cfg.scheduler.enabled:
                from hosts.web.app_support.cron_delivery import ThreadTargetSink, WebDeliverySink
                from hosts.web.websocket.cron import get_broker
                from scheduler.delivery import DeliveryDispatcher

                cron_dispatcher = DeliveryDispatcher(
                    web_sink=WebDeliverySink(get_broker()),
                    target_sink=ThreadTargetSink(thread_manager)
                    if thread_manager is not None
                    else None,
                )

            def _scheduler_runtime_factory(store):  # type: ignore[no-untyped-def]
                from scheduler.runtime_factory import build_cron_execution_bridge

                return build_cron_execution_bridge(
                    real_cfg,
                    store,
                    event_sinks=sink_list,
                    tools=registry,
                    enabled_tool_names=enabled_tool_names,
                    instructions=_instructions_cache[0],
                    dispatcher=cron_dispatcher,
                    preset_map=_current_preset_map(),
                )

            register_schedule_tool_if_enabled(
                registry,
                real_cfg,
                runtime_factory_fn=_scheduler_runtime_factory,
                default_preset_id=next(iter(_current_preset_map()), ""),
                thread_provisioner=thread_manager,
            )
            register_evolution_write_tool_if_enabled(
                registry,
                real_cfg,
                event_sinks=sink_list,
            )
            register_choice_tool(registry, event_sinks=sink_list)
            register_task_progress_tool(registry, real_cfg)
            mcp_runtime_registration = McpRuntimeRegistrationManager(
                real_cfg,
                event_sinks=sink_list,
            )
            await mcp_runtime_registration.register(
                registry,
                excluded_tool_names=("evolution_write",),
            )
            enabled_tool_names = [name for name in registry.names() if name != "evolution_write"]

            _registry_cache[0] = registry
            _enabled_tools_cache[0] = enabled_tool_names
            _agent_workflow_handle_cache[0] = agent_workflow_handle
            _agent_role_manager_cache[0] = agent_role_manager
            _instructions_cache[0] = rendered
            _origins_cache[0] = origins
            _scheduler_runtime_factory_cache[0] = _scheduler_runtime_factory
            _mcp_runtime_registration_cache[0] = mcp_runtime_registration
            _workflow_prompt_cache_key[0] = workflow_cache_key

    async def factory(
        thread_id: str,
        preset_id: str,
        adapter: object,
        sinks: object,
    ) -> tuple[Any, Any]:
        from infrastructure.config.paths import resolve_kongming_path

        preset = _current_preset_map().get(preset_id)
        if preset is None:
            raise ValueError(f"unknown preset_id: {preset_id!r}")

        if isinstance(sinks, list):
            trace_path = resolve_kongming_path(real_cfg.trace.output_path, kongming_home=home)
            stem = trace_path.stem
            suffix = trace_path.suffix
            per_thread_path = trace_path.with_name(f"{stem}.{thread_id}{suffix}")
            sinks.append(
                JsonlTraceSink(
                    per_thread_path,
                    auto_flush=real_cfg.trace.auto_flush,
                )
            )

        api_key = os.environ.get(preset.api_key_env, "") if preset.api_key_env else ""
        model_overrides: dict[str, Any] = {
            "name": preset.model,
            "base_url": preset.base_url,
            "api_key": api_key,
        }
        if preset.provider is not None:
            model_overrides["provider"] = preset.provider
        if preset.reasoning_effort is not None:
            model_overrides["reasoning_effort"] = preset.reasoning_effort
        preset_model = real_cfg.model.model_copy(update=model_overrides)
        preset_cfg = real_cfg.model_copy(update={"model": preset_model})

        await _ensure_shared_assets(sinks)
        factory._workflow_prompt_cache_key = _workflow_prompt_cache_key[0]  # type: ignore[attr-defined]
        factory._scheduler_runtime_factory = _scheduler_runtime_factory_cache[0]  # type: ignore[attr-defined]
        factory._mcp_runtime_registration = _mcp_runtime_registration_cache[0]  # type: ignore[attr-defined]
        instructions = _instructions_cache[0]
        assert instructions is not None
        origins = _origins_cache[0] or []
        registry = _registry_cache[0]
        assert registry is not None
        enabled_tool_names = _enabled_tools_cache[0] or []
        agent_workflow_handle = _agent_workflow_handle_cache[0]
        agent_role_manager = _agent_role_manager_cache[0]
        assert agent_workflow_handle is not None
        assert agent_role_manager is not None

        # 阶段 1 (smart-approval-manager-v0.5)：generic_chat 通道走 ApprovalManager
        # 路径（无 feature flag；回滚 = git revert）。manager + InboxEventSink 是
        # per-process 单例，首次装配时注入 sink；后续 cell 装配复用已有单例（幂等）。
        # 老路径（adapter.prompt_approval 推 per-thread modal）已被 v2-inbox 全局
        # 浮窗替代，prompt_fn 现在通过 manager.request 调度，前端通过
        # /ws/thread-status 收 approval.inbox.add 帧。
        #
        # task #6：app 引用从 _make_runtime_factory 调用方（main()）通过 attr 回挂
        # （``setattr(runtime_factory, "_app", app)``）；首次 factory 调用时已就绪。
        # 注入 ConfigStore 让 generic_chat 通道能查 cwd 自动通过配置。
        #
        # approval-rules-unified：
        # - manager 默认 timeout 60s（与 host_adapter 默认对齐），不再读 cfg
        #   ``pending_approval_timeout_seconds``（仍归 host_adapter 用）。
        # - ApprovalRules 直接拿 ``app.state.auto_approval_policy`` 完整实例，
        #   走 24 规则 + per-cwd 总开关统一真源。
        #
        # task #4 (thread-cwd-fallback)：
        # - ``default_cwd`` 由 thread metadata 解析：thread.cwd 非空直接用，
        #   空时 fallback 到 server 启动目录（``app.state.workspace_root``），
        #   交给 prompt_fn 作为 ``req.metadata.cwd`` 空时的兜底——保证 generic_chat
        #   通道在「纯聊天 thread 未绑 cwd」场景下仍能命中 cwd 自动通过规则。
        app_ref = getattr(factory, "_app", None)
        default_cwd = _resolve_default_cwd_for_thread(app_ref, thread_id)
        manager = _build_manager_and_inbox_sink(app=app_ref)
        prompt_fn = (
            make_manager_prompt_fn(
                manager,
                thread_id,
                default_cwd=default_cwd,
            )
            if real_cfg.approval.mode == "interactive"
            else None
        )
        approval = build_default_approval(real_cfg.approval.mode, prompt_fn=prompt_fn)

        bootstrap = SessionBootstrap(
            agent_name="kongming-agent",
            model_name=preset_cfg.model.name,
            instruction_sources=origins,
            instruction_text_hash=f"sha256:{hashlib.sha256(instructions.encode()).hexdigest()}",
            created_at=time.time(),
            cwd=default_cwd,
            instruction_text=instructions,
        )

        def session_factory(sid: str) -> Any:
            return build_session(preset_cfg, sid, bootstrap=bootstrap)

        runtime = NativeRuntime.build(
            preset_cfg,
            event_sinks=list(sinks) if sinks else [],  # type: ignore[call-overload]
            approval=approval,
            tools=registry,
            enabled_tool_names=enabled_tool_names,
            session_factory=session_factory,
            instructions=instructions,
        )
        _bind_agent_workflow_manager(
            handle=agent_workflow_handle,
            thread_id=thread_id,
            runtime=runtime,
            config=preset_cfg,
            workspace_root=Path(default_cwd),
            role_manager=agent_role_manager,
            tool_registry=registry,
        )
        bridge = SessionBridge(
            runtime=runtime,
            adapter=adapter,  # type: ignore[arg-type]
            session_id=thread_id,
            echo_final_content=False,
        )
        return runtime, bridge

    factory._scheduler_runtime_factory = _scheduler_runtime_factory_cache[0]  # type: ignore[attr-defined]
    factory._mcp_runtime_registration = _mcp_runtime_registration_cache[0]  # type: ignore[attr-defined]
    return factory


def _register_agent_workflow_tools(registry: Any, *, role_dir: Path) -> tuple[Any, Any]:
    """注册 Web generic_chat 的 agent workflow 工具，输入为 registry 和角色目录，输出角色 manager 与 workflow handle。"""
    from application.agent_roles import AgentRoleManager
    from tools import register_agent_role_tool, register_agent_workflow_tool
    from tools.agent_workflow_tool import AgentWorkflowHandle

    role_manager = AgentRoleManager(role_dir=role_dir)
    workflow_handle = AgentWorkflowHandle()
    register_agent_role_tool(registry, role_manager)
    register_agent_workflow_tool(registry, workflow_handle)
    return role_manager, workflow_handle


def _bind_agent_workflow_manager(
    *,
    handle: Any,
    thread_id: str,
    runtime: Any,
    config: Any,
    workspace_root: Path,
    role_manager: Any,
    tool_registry: Any,
) -> None:
    """把当前 Web thread runtime 绑定到 workflow handle，输入为运行时和工作区，输出为可执行 workflow manager。"""
    from application.agent_workflows.manager import AgentWorkflowManager
    from application.subagents.manager import SubAgentManager
    from hosts.web.research_source_provider import WebResearchSourceProviderFactory

    source_provider_result = WebResearchSourceProviderFactory(config).build(tool_registry)
    diagnostics = source_provider_result.diagnostics
    if source_provider_result.provider is None:
        logger.info(
            "deep_research web source provider unavailable: reason=%s fallback=%s",
            diagnostics.reason,
            diagnostics.fallback_reason,
        )
    else:
        logger.info(
            "deep_research web source provider enabled: provider=%s search_tool=%s fetch_tool=%s",
            diagnostics.provider_name,
            diagnostics.search_tool_name,
            diagnostics.fetch_tool_name,
        )

    handle.bind(
        AgentWorkflowManager(
            subagents=SubAgentManager(runtime),
            config=config,
            workspace_root=workspace_root,
            role_manager=role_manager,
            deep_research_source_provider=source_provider_result.provider,
            deep_research_source_diagnostics=diagnostics,
        ),
        session_id=thread_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
