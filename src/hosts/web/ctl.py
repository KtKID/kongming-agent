"""Web 服务进程管理命令。

功能：
    提供 `kongming-web-ctl start|stop|restart|status|log`，管理 Web 后端进程。

作用：
    让 CLI、本地开发脚本和桌面宿主通过同一个控制入口读取状态、停止进程和查看日志。

关键执行流程：
    1. 解析 `--home`，定位运行时目录。
    2. 将仓库 `.env` 的运行时变量合并到 `KONGMING_HOME/.env`。
    3. start 读取配置态 host / port，stop/status 读取运行态 host / port / pid。
    4. 通过端口、pid 文件和 HTTP runtime-status 判断服务状态。
    5. start / stop / restart / log 围绕同一运行时目录操作。

关键函数：
    `_resolve_home`：解析运行时 home，并写入 `KONGMING_HOME`。
    `_read_server_info`：读取 `<home>/web/server.json`。
    `_sync_repo_dotenv_to_home`：把仓库 `.env` 中的运行时变量合并进 home `.env`。
    `_read_configured_port` / `_read_configured_host`：解析 start 使用的配置态端口和 host。
    `_read_port` / `_read_host`：解析 stop/status 使用的运行态端口和 host。
    `main`：console script 入口。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ParamSpec, TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
from dotenv import dotenv_values

_REPO_ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger(__name__)
_ENTRYPOINT_ENV_KEYS = ("KONGMING_HOME", "KONGMING_CONFIG")
_RUNTIME_ENV_EXCLUDED_KEYS = frozenset(_ENTRYPOINT_ENV_KEYS)


def _dotenv_skip_enabled() -> bool:
    """判断当前进程是否显式禁用 dotenv 引导。"""
    return os.environ.get("KONGMING_SKIP_DOTENV", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
        "y",
        "t",
    }


def _read_repo_dotenv_values() -> dict[str, str]:
    """读取仓库 `.env` 中的字符串键值。"""
    if _dotenv_skip_enabled():
        logger.debug("ctl: skipping repo dotenv due to KONGMING_SKIP_DOTENV")
        return {}
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        logger.debug("ctl: repo .env not present at %s", env_path)
        return {}
    try:
        values = dotenv_values(env_path)
    except OSError as exc:
        logger.warning("ctl: failed to read repo dotenv at %s: %s", env_path, exc)
        return {}
    return {key: value for key, value in values.items() if isinstance(value, str)}


def _load_repo_entrypoint_env() -> None:
    """从仓库 `.env` 只读取入口级 env。

    仓库 `.env` 仅负责把 ctl 指向目标 runtime home 或显式配置文件；provider key
    与 Web 可写配置统一由 `KONGMING_HOME/.env` 和 YAML 读取。
    """
    if _dotenv_skip_enabled():
        logger.debug("ctl: skipping repo entrypoint env due to KONGMING_SKIP_DOTENV")
        return
    values = _read_repo_dotenv_values()
    if not values:
        return
    for key in _ENTRYPOINT_ENV_KEYS:
        value = values.get(key)
        if isinstance(value, str) and value.strip() and key not in os.environ:
            os.environ[key] = value


_load_repo_entrypoint_env()


from hosts.web.app_support.startup_progress import StartupProgress  # noqa: E402
from hosts.web.auth.middleware import (  # noqa: E402
    SESSION_COOKIE_NAME,
    SessionTokenPayload,
    make_serializer,
)
from hosts.web.auth.secrets import load_or_init_session_secret  # noqa: E402
from infrastructure.config import load_config  # noqa: E402
from infrastructure.config.paths import get_kongming_home  # noqa: E402

_STARTUP_WAIT_SECONDS = 30
_FRONTEND_SOURCE_EXTENSIONS = (".ts", ".tsx", ".css", ".html", ".json")
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _home_option(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """为 click 子命令增加 `--home` 参数。"""
    decorator = click.option(
        "--home",
        type=click.Path(path_type=Path),
        default=None,
        help="Kongming runtime home directory.",
    )
    return decorator(func)


def _resolve_home(explicit_home: Path | None) -> Path:
    """解析运行时 home。

    Args:
        explicit_home: 命令行传入的 home。

    Returns:
        已归一化的 home 路径。
    """
    if explicit_home is not None:
        home = explicit_home.expanduser().resolve()
        os.environ["KONGMING_HOME"] = str(home)
        return home
    return get_kongming_home()


def _pid_file(home: Path) -> Path:
    """返回当前 home 下的 pid 文件路径。"""
    return home / "web" / "server.pid"


def _log_file(home: Path) -> Path:
    """返回当前 home 下的日志文件路径。"""
    return home / "web" / "server.log"


def _server_json(home: Path) -> Path:
    """返回当前 home 下的 server.json 路径。"""
    return home / "web" / "server.json"


def _ensure_dirs(home: Path) -> None:
    """创建当前 home 下的 Web 运行目录。"""
    (home / "web").mkdir(parents=True, exist_ok=True)


def _is_runtime_env_key(key: str) -> bool:
    """判断一个 `.env` key 是否适合复制到运行时 home。"""
    if key in _RUNTIME_ENV_EXCLUDED_KEYS:
        return False
    if not key:
        return False
    first = key[0]
    if not (first.isalpha() or first == "_"):
        return False
    return all(ch.isupper() or ch.isdigit() or ch == "_" for ch in key)


def _sync_repo_dotenv_to_home(home: Path) -> list[str]:
    """把仓库 `.env` 的运行时变量合并进 `<home>/.env`。

    已存在且非空的 home 变量保持不变；仓库 `.env` 中的入口指针和非法 key 不复制。
    """
    repo_values = _read_repo_dotenv_values()
    if not repo_values:
        return []

    home_env = home / ".env"
    try:
        existing_values = dotenv_values(home_env) if home_env.exists() else {}
    except OSError as exc:
        logger.warning("ctl: failed to read home dotenv at %s: %s", home_env, exc)
        existing_values = {}

    updates: dict[str, str] = {}
    for key, value in repo_values.items():
        if not _is_runtime_env_key(key) or not value.strip():
            continue
        existing_value = existing_values.get(key)
        if isinstance(existing_value, str) and existing_value.strip():
            continue
        updates[key] = value

    if not updates:
        return []

    from infrastructure.config.env_writer import write_env_values

    result = write_env_values(home_env, updates)
    logger.info(
        "ctl: synced repo dotenv keys to %s: %s",
        result.path,
        ", ".join(result.updated_keys),
    )
    return result.updated_keys


def _read_server_info(home: Path) -> dict[str, Any] | None:
    """读取 sidecar ready 写入的 server.json。

    Args:
        home: 运行时 home。

    Returns:
        读取成功时返回 JSON dict，缺失或格式错误时返回 None。
    """
    path = _server_json(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _server_info_int(server_info: dict[str, Any] | None, key: str) -> int | None:
    """从 server info 中读取整数字段。"""
    if server_info is None:
        return None
    value = server_info.get(key)
    if value is None:
        return None
    with contextlib.suppress(TypeError, ValueError):
        return int(value)
    return None


def _server_info_str(server_info: dict[str, Any] | None, key: str) -> str | None:
    """从 server info 中读取非空字符串字段。"""
    if server_info is None:
        return None
    value = server_info.get(key)
    return value if isinstance(value, str) and value else None


def _resolve_config_path(home: Path) -> Path:
    """解析 ctl 使用的配置路径。"""
    env_config = os.environ.get("KONGMING_CONFIG")
    if env_config and env_config.strip():
        return Path(env_config).expanduser().resolve()

    from infrastructure.config.paths import find_existing_kongming_home_config

    home_config = find_existing_kongming_home_config(home)
    if home_config is not None:
        os.environ["KONGMING_CONFIG"] = str(home_config)
        return home_config

    return _REPO_ROOT / "config" / "setting.yaml"


def _read_port(home: Path) -> int:
    """读取当前 home 对应的 Web 端口。"""
    port = _server_info_int(_read_server_info(home), "port")
    if port is not None:
        return port
    return _read_configured_port(home)


def _read_host(home: Path) -> str:
    """读取当前 home 对应的 Web host。"""
    host = _server_info_str(_read_server_info(home), "host")
    if host is not None:
        return host
    return _read_configured_host(home)


def _read_configured_port(home: Path) -> int:
    """读取配置态 Web 端口，供 start 选择目标端口。"""
    return int(load_config(_resolve_config_path(home)).web.port)


def _read_configured_host(home: Path) -> str:
    """读取配置态 Web host，供 start 选择目标监听地址。"""
    return str(load_config(_resolve_config_path(home)).web.host)


def _issue_local_status_cookie(home: Path) -> str:
    """签发本机 status 查询 cookie。"""
    secret = load_or_init_session_secret(home)
    serializer = make_serializer(secret)
    payload = SessionTokenPayload(iat=int(time.time()))
    return serializer.dumps(payload.model_dump())


def _fetch_runtime_status(port: int, *, home: Path) -> dict[str, Any] | None:
    """查询 Web 运行时状态。"""
    request = Request(
        f"http://127.0.0.1:{port}/api/manage/runtime-status",
        headers={
            "Cookie": f"{SESSION_COOKIE_NAME}={_issue_local_status_cookie(home)}",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=2.0) as response:
            body = response.read().decode("utf-8")
    except (HTTPError, URLError, OSError, ValueError):
        return None
    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def _port_probe_host(host: str) -> str:
    """把监听 host 转成本机可连接地址，输入监听地址，输出探测地址。"""
    if host in {"0.0.0.0", ""}:
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    host = _port_probe_host(host)
    try:
        addr_infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror):
        return False
    for family, socktype, proto, _canonname, sockaddr in addr_infos:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(0.5)
        try:
            if sock.connect_ex(sockaddr) == 0:
                return True
        except OSError:
            continue
        finally:
            sock.close()
    return False


def _find_pid_by_port(port: int) -> int | None:
    if sys.platform == "win32":
        return _find_pid_by_port_win32(port)
    return _find_pid_by_port_lsof(port)


def _find_pid_by_port_win32(port: int) -> int | None:
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            # 中文 Windows 的 netstat 输出为系统 ANSI 代码页（GBK/CP936），
            # 含非 UTF-8 字节会让默认 text reader 线程抛 UnicodeDecodeError，
            # 进而 out.stdout 变成 None。errors="replace" 保证不崩，而 IP/端口/PID/
            # LISTENING 关键字段均为 ASCII，解析不受影响。
            errors="replace",
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or out.stdout is None:
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        if parts[1].endswith(f":{port}"):
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


def _find_pid_by_port_lsof(port: int) -> int | None:
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return int(out.stdout.strip().splitlines()[0])
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    """判断 pid 是否仍存活。"""
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                # 中文 Windows 的 tasklist 输出为系统 ANSI 代码页（GBK/CP936），
                # 含非 UTF-8 字节会让默认 text reader 线程抛 UnicodeDecodeError，
                # 进而 out.stdout 变成 None。errors="replace" 保证不崩。
                errors="replace",
                timeout=3,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return out.returncode == 0 and out.stdout is not None and str(pid) in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError, SystemError):
        return False


def _resolve_running_pid(port: int, *, home: Path) -> int | None:
    """解析当前监听进程 pid。"""
    if sys.platform == "win32":
        port_pid = _find_pid_by_port(port)
        if port_pid is not None:
            return port_pid

    pid = _server_info_int(_read_server_info(home), "pid")
    if pid is not None and _pid_alive(pid):
        return pid

    pid_file = _pid_file(home)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
        if pid is not None and _pid_alive(pid):
            return pid
    return _find_pid_by_port(port)


def _persist_running_pid(started_pid: int, port: int, *, home: Path) -> int:
    """Persist the real listener pid when it differs from the shell pid.

    Windows may report a bootstrap shell pid from ``Popen`` while the actual
    listening socket belongs to a child ``python -m hosts.web.run`` process. Persist
    the listener pid once the port is up so later status/restart operations
    observe the same process id users see from the listening port.
    """
    pid = _find_pid_by_port(port) or started_pid
    pid_file = _pid_file(home)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(pid), encoding="utf-8")
    return pid


def _tail_log(n: int, *, home: Path) -> None:
    """输出日志尾部。"""
    log_file = _log_file(home)
    try:
        result = subprocess.run(
            ["tail", f"-{n}", str(log_file)],
            capture_output=True,
            text=True,
            timeout=3,
        )
        click.echo(result.stdout, nl=False, err=True)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _should_rebuild_dist(*, force: bool) -> bool:
    web_dir = _REPO_ROOT / "web"
    dist_index = web_dir / "dist" / "index.html"

    if force:
        click.echo("Frontend rebuild forced (--rebuild)...")
        return True
    if not dist_index.exists():
        click.echo("Frontend dist not found, building...")
        return True

    dist_mtime = dist_index.stat().st_mtime
    candidate_dirs = [web_dir / "src"]
    candidate_files = [
        web_dir / "package.json",
        web_dir / "vite.config.ts",
        web_dir / "tsconfig.json",
        web_dir / "index.html",
    ]
    for src_dir in candidate_dirs:
        if not src_dir.exists():
            continue
        for path in src_dir.rglob("*"):
            if not path.is_file() or path.suffix not in _FRONTEND_SOURCE_EXTENSIONS:
                continue
            if "__tests__" in path.parts:
                continue
            if path.stat().st_mtime > dist_mtime:
                click.echo(
                    f"Frontend stale (newer source: {path.relative_to(_REPO_ROOT)}), rebuilding..."
                )
                return True
    for cfg in candidate_files:
        if cfg.exists() and cfg.stat().st_mtime > dist_mtime:
            click.echo(
                f"Frontend stale (newer config: {cfg.relative_to(_REPO_ROOT)}), rebuilding..."
            )
            return True
    return False


def _spawn_creationflags() -> int:
    if sys.platform != "win32":
        return 0
    return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS


def _frontend_build_command() -> list[str]:
    if sys.platform == "win32":
        npm_cmd = shutil.which("npm.cmd")
        if npm_cmd:
            return [npm_cmd, "run", "build"]
    return ["npm", "run", "build"]


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Manage the web service process."""


@cli.command()
@click.option(
    "--rebuild", is_flag=True, default=False, help="Force rebuild frontend dist before start."
)
@_home_option
def start(rebuild: bool, home: Path | None) -> None:
    runtime_home = _resolve_home(home)
    _ensure_dirs(runtime_home)
    progress = StartupProgress(runtime_home)
    progress.report("env")
    _sync_repo_dotenv_to_home(runtime_home)

    port = _read_configured_port(runtime_home)
    host = _read_configured_host(runtime_home)

    existing = _find_pid_by_port(port)
    if existing is not None:
        click.echo(f"Already running (PID {existing}, port {port})")
        click.echo("Use 'restart' to restart.")
        return
    progress.report("port")

    progress.report("frontend")
    if _should_rebuild_dist(force=rebuild):
        try:
            subprocess.run(_frontend_build_command(), cwd=_REPO_ROOT / "web", check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            progress.fail(f"Frontend build failed: {exc}")
            click.echo(f"Frontend build failed: {exc}", err=True)
            raise SystemExit(1) from exc

    click.echo(f"Starting web server on {host}:{port}...")
    log_file = _log_file(runtime_home)
    with open(log_file, "ab") as log_handle:
        popen_kwargs: dict[str, Any] = {
            "cwd": _REPO_ROOT,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "env": os.environ.copy(),
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = _spawn_creationflags()
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen([sys.executable, "-m", "hosts.web.run"], **popen_kwargs)

    _pid_file(runtime_home).write_text(str(proc.pid), encoding="utf-8")

    for _ in range(_STARTUP_WAIT_SECONDS):
        if not _pid_alive(proc.pid):
            progress.fail("Web process died during startup")
            click.echo("Failed to start. Last log lines:", err=True)
            _tail_log(20, home=runtime_home)
            _pid_file(runtime_home).unlink(missing_ok=True)
            raise SystemExit(1)
        if _is_port_listening(port):
            running_pid = _persist_running_pid(proc.pid, port, home=runtime_home)
            click.echo(f"Started (PID {running_pid})")
            click.echo(f"  URL: http://localhost:{port}")
            click.echo(f"  Log: {log_file}")
            return
        time.sleep(1)

    click.echo(f"Still starting after {_STARTUP_WAIT_SECONDS}s... check {log_file}")


@cli.command()
@_home_option
def stop(home: Path | None) -> None:
    runtime_home = _resolve_home(home)
    port = _read_port(runtime_home)
    pid = _resolve_running_pid(port, home=runtime_home)
    if pid is None:
        click.echo("Not running")
        _pid_file(runtime_home).unlink(missing_ok=True)
        return

    click.echo(f"Stopping (PID {pid})...")
    if sys.platform == "win32":
        _stop_win32(pid, home=runtime_home)
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _pid_file(runtime_home).unlink(missing_ok=True)
        click.echo("Stopped (already gone)")
        return

    for _ in range(5):
        if not _pid_alive(pid):
            click.echo("Stopped")
            _pid_file(runtime_home).unlink(missing_ok=True)
            return
        time.sleep(1)

    click.echo("Force killing...")
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    _pid_file(runtime_home).unlink(missing_ok=True)
    click.echo("Stopped (force)")


def _stop_win32(pid: int, *, home: Path) -> None:
    """Windows 上停止进程树。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    _pid_file(home).unlink(missing_ok=True)
    click.echo("Stopped")


@cli.command()
@click.option(
    "--rebuild", is_flag=True, default=False, help="Force rebuild frontend dist during restart."
)
@_home_option
@click.pass_context
def restart(ctx: click.Context, rebuild: bool, home: Path | None) -> None:
    runtime_home = _resolve_home(home)
    ctx.invoke(stop, home=runtime_home)
    time.sleep(1)
    ctx.invoke(start, rebuild=rebuild, home=runtime_home)


@cli.command()
@_home_option
def status(home: Path | None) -> None:
    runtime_home = _resolve_home(home)
    port = _read_port(runtime_home)
    host = _read_host(runtime_home)
    pid = _resolve_running_pid(port, home=runtime_home)
    if pid is None:
        click.echo("Not running")
        _pid_file(runtime_home).unlink(missing_ok=True)
        return
    if _is_port_listening(port, host=host):
        click.echo(f"Running (PID {pid}, port {port})")
        server_info = _read_server_info(runtime_home) or {}
        base_url = server_info.get("base_url") or f"http://localhost:{port}"
        click.echo(f"  URL: {base_url}")
    else:
        click.echo(f"Starting (PID {pid}, port {port})")
        click.echo("  Port is not listening yet")
    click.echo(f"  Log: {_log_file(runtime_home)}")

    if not _is_port_listening(port, host=host):
        return

    snapshot = _fetch_runtime_status(port, home=runtime_home)
    if snapshot is None:
        return

    polling = snapshot.get("polling", {})
    global_ws = snapshot.get("global_ws", {})
    provider_sessions = snapshot.get("provider_sessions", {})
    click.echo("  Runtime:")
    click.echo(f"    dashboard_poll={polling.get('interval_seconds', '?')}s")
    click.echo(
        "    cells={cells} chat_ws={chat_ws} approvals={approvals}".format(
            cells=snapshot.get("cells_total", 0),
            chat_ws=snapshot.get("chat_ws_connections_total", 0),
            approvals=snapshot.get("approval_pending_total", 0),
        )
    )
    click.echo(
        "    thread_status_ws={thread_status} cron_ws={cron} approval_subscribers={subs}".format(
            thread_status=global_ws.get("thread_status_connections", 0),
            cron=global_ws.get("cron_connections", 0),
            subs=global_ws.get("approval_subscribers", 0),
        )
    )
    click.echo(
        "    claude_sessions={claude} codex_sessions={codex}".format(
            claude=provider_sessions.get("claude_active_sessions", 0),
            codex=provider_sessions.get("codex_active_sessions", 0),
        )
    )
    cells = snapshot.get("cells", [])
    if cells:
        click.echo("    active_cells:")
        for cell in cells:
            click.echo(
                "      - {thread_id} [{status}] ws={chat_ws} pending={pending} name={name}".format(
                    thread_id=cell.get("thread_id", "-"),
                    status=cell.get("status", "-"),
                    chat_ws=cell.get("chat_ws_connections", 0),
                    pending=cell.get("pending_approval_count", 0),
                    name=cell.get("thread_name", "-"),
                )
            )


@cli.command()
@_home_option
def log(home: Path | None) -> None:
    runtime_home = _resolve_home(home)
    log_file = _log_file(runtime_home)
    if not log_file.exists():
        click.echo(f"No log file at {log_file}")
        return
    with contextlib.suppress(KeyboardInterrupt):
        subprocess.run(["tail", "-f", str(log_file)], check=False)


def main() -> None:
    """console script 入口。"""
    cli()


if __name__ == "__main__":
    main()
