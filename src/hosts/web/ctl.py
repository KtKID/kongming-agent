"""Process manager for the web service."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

from hosts.web.app_support.startup_progress import StartupProgress  # noqa: E402
from hosts.web.auth.middleware import (  # noqa: E402
    SESSION_COOKIE_NAME,
    SessionTokenPayload,
    make_serializer,
)
from hosts.web.auth.secrets import load_or_init_session_secret  # noqa: E402
from infrastructure.config import load_config  # noqa: E402
from infrastructure.config.paths import get_kongming_home  # noqa: E402

_PID_FILE = _REPO_ROOT / ".kongming" / "web" / "server.pid"
_LOG_FILE = _REPO_ROOT / ".kongming" / "web" / "server.log"
_STARTUP_WAIT_SECONDS = 30
_FRONTEND_SOURCE_EXTENSIONS = (".ts", ".tsx", ".css", ".html", ".json")


def _ensure_dirs() -> None:
    (_REPO_ROOT / ".kongming" / "web").mkdir(parents=True, exist_ok=True)


def _read_port() -> int:
    return int(load_config().web.port)


def _read_host() -> str:
    return str(load_config().web.host)


def _issue_local_status_cookie() -> str:
    secret = load_or_init_session_secret(get_kongming_home())
    serializer = make_serializer(secret)
    payload = SessionTokenPayload(iat=int(time.time()))
    return serializer.dumps(payload.model_dump())


def _fetch_runtime_status(port: int) -> dict[str, Any] | None:
    request = Request(
        f"http://127.0.0.1:{port}/api/manage/runtime-status",
        headers={
            "Cookie": f"{SESSION_COOKIE_NAME}={_issue_local_status_cookie()}",
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


def _is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


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
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
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
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError, SystemError):
        return False


def _resolve_running_pid(port: int) -> int | None:
    if sys.platform == "win32":
        port_pid = _find_pid_by_port(port)
        if port_pid is not None:
            return port_pid

    if _PID_FILE.exists():
        try:
            pid = int(_PID_FILE.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
        if pid is not None and _pid_alive(pid):
            return pid
    return _find_pid_by_port(port)


def _persist_running_pid(started_pid: int, port: int) -> int:
    """Persist the real listener pid when it differs from the shell pid.

    Windows may report a bootstrap shell pid from ``Popen`` while the actual
    listening socket belongs to a child ``python -m hosts.web.run`` process. Persist
    the listener pid once the port is up so later status/restart operations
    observe the same process id users see from the listening port.
    """
    pid = _find_pid_by_port(port) or started_pid
    _PID_FILE.write_text(str(pid), encoding="utf-8")
    return pid


def _tail_log(n: int) -> None:
    try:
        result = subprocess.run(
            ["tail", f"-{n}", str(_LOG_FILE)],
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
def start(rebuild: bool) -> None:
    _ensure_dirs()
    progress = StartupProgress(_REPO_ROOT / ".kongming")
    progress.report("env")

    port = _read_port()
    host = _read_host()

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
    with open(_LOG_FILE, "ab") as log_handle:
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

    _PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    for _ in range(_STARTUP_WAIT_SECONDS):
        if not _pid_alive(proc.pid):
            progress.fail("Web process died during startup")
            click.echo("Failed to start. Last log lines:", err=True)
            _tail_log(20)
            _PID_FILE.unlink(missing_ok=True)
            raise SystemExit(1)
        if _is_port_listening(port):
            running_pid = _persist_running_pid(proc.pid, port)
            click.echo(f"Started (PID {running_pid})")
            click.echo(f"  URL: http://localhost:{port}")
            click.echo(f"  Log: {_LOG_FILE}")
            return
        time.sleep(1)

    click.echo(f"Still starting after {_STARTUP_WAIT_SECONDS}s... check {_LOG_FILE}")


@cli.command()
def stop() -> None:
    port = _read_port()
    pid = _resolve_running_pid(port)
    if pid is None:
        click.echo("Not running")
        _PID_FILE.unlink(missing_ok=True)
        return

    click.echo(f"Stopping (PID {pid})...")
    if sys.platform == "win32":
        _stop_win32(pid)
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _PID_FILE.unlink(missing_ok=True)
        click.echo("Stopped (already gone)")
        return

    for _ in range(5):
        if not _pid_alive(pid):
            click.echo("Stopped")
            _PID_FILE.unlink(missing_ok=True)
            return
        time.sleep(1)

    click.echo("Force killing...")
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    _PID_FILE.unlink(missing_ok=True)
    click.echo("Stopped (force)")


def _stop_win32(pid: int) -> None:
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
    _PID_FILE.unlink(missing_ok=True)
    click.echo("Stopped")


@cli.command()
@click.option(
    "--rebuild", is_flag=True, default=False, help="Force rebuild frontend dist during restart."
)
@click.pass_context
def restart(ctx: click.Context, rebuild: bool) -> None:
    ctx.invoke(stop)
    time.sleep(1)
    ctx.invoke(start, rebuild=rebuild)


@cli.command()
def status() -> None:
    port = _read_port()
    pid = _resolve_running_pid(port)
    if pid is None:
        click.echo("Not running")
        _PID_FILE.unlink(missing_ok=True)
        return
    if _is_port_listening(port):
        click.echo(f"Running (PID {pid}, port {port})")
        click.echo(f"  URL: http://localhost:{port}")
    else:
        click.echo(f"Starting (PID {pid}, port {port})")
        click.echo("  Port is not listening yet")
    click.echo(f"  Log: {_LOG_FILE}")

    if not _is_port_listening(port):
        return

    snapshot = _fetch_runtime_status(port)
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
def log() -> None:
    if not _LOG_FILE.exists():
        click.echo(f"No log file at {_LOG_FILE}")
        return
    with contextlib.suppress(KeyboardInterrupt):
        subprocess.run(["tail", "-f", str(_LOG_FILE)], check=False)


if __name__ == "__main__":
    cli()
