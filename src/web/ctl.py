"""Web 服务进程管理 CLI（替代 ``scripts/web-ctl.sh``）。

子命令：

- ``start``：检查端口未被占用 → 起后台 web 进程 → 等待端口监听上
- ``stop``：找 PID → SIGTERM → 等死 → 必要时 SIGKILL
- ``restart``：``stop`` + ``start``
- ``status``：报当前是否运行 + PID + URL
- ``log``：``tail -f`` 服务日志

复用项目栈：

- ``python-dotenv`` 自动加载 ``.env``（弥补 bash 看不到 env 的洞）
- ``config_loader.load_config`` 读 ``setting.yaml`` + env 覆盖链路，
  port/host 来自 ``cfg.web``，与 ``web.run`` 完全一致
- 标准库 ``subprocess`` + ``os.kill`` + ``socket`` 跨平台管进程，
  ``lsof`` 子进程作为找端口监听者的 fallback（mac/linux 都自带）

调用方式（保持 ``./start.sh web ...`` 用户接口不变）：

```bash
./start.sh web start    # → bash 薄壳调 uv run python -m web.ctl start
./start.sh web stop
./start.sh web restart
./start.sh web status
./start.sh web log
```
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import click
from dotenv import load_dotenv

# 必须在 import config_loader 之前加载 .env，确保 KONGMING_* env 注入
# 进程环境，下游 load_config 才能消费。
_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

from config_loader import load_config  # noqa: E402  # dotenv 加载后才 import
from web.startup_progress import StartupProgress  # noqa: E402

# 进程 + 日志文件路径（与原 web-ctl.sh 对齐）
_PID_FILE = _REPO_ROOT / ".kongming" / "web" / "server.pid"
_LOG_FILE = _REPO_ROOT / ".kongming" / "web" / "server.log"


# ---------------------------------------------------------------------------
# 内部 helper
# ---------------------------------------------------------------------------


def _ensure_dirs() -> None:
    """确保 ``.kongming/web/`` 目录存在（pid / log 落盘点）。"""
    (_REPO_ROOT / ".kongming" / "web").mkdir(parents=True, exist_ok=True)


def _read_port() -> int:
    """从 setting.yaml + env 覆盖链路读 web.port（单一真源）。"""
    cfg = load_config()
    return int(cfg.web.port)


def _read_host() -> str:
    """从 setting.yaml + env 覆盖链路读 web.host。"""
    cfg = load_config()
    return str(cfg.web.host)


def _is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    """检查端口是否有监听者。

    用 socket connect 探测：能连上 = 有监听；refused = 无监听。比 lsof
    更快（不 spawn 子进程），且无需特权。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        result = sock.connect_ex((host, port))
        return result == 0
    finally:
        sock.close()


def _find_pid_by_port(port: int) -> int | None:
    """通过 ``lsof`` 找占用指定端口的进程 PID。

    mac / linux 都自带 lsof；Windows 暂不支持（没有 lsof，需要 netstat
    回退，本项目目前不是 Windows-first，留 v0.2 处理）。
    """
    try:
        # -ti 安静模式只输出 PID；-iTCP:N 限定 TCP 端口 N；-sTCP:LISTEN 只看 listen 态
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        # 多个 PID 时取第一个（一般 nohup 后只一个）
        return int(out.stdout.strip().splitlines()[0])
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` 不发信号，仅探测进程存活。"""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# 前端 dist stale 检测：扫描这些后缀的源文件 mtime 与 dist 比对
_FRONTEND_SOURCE_EXTENSIONS = (".ts", ".tsx", ".css", ".html", ".json")


def _should_rebuild_dist(*, force: bool) -> bool:
    """判定是否需要 ``npm run build``。

    三档触发：

    1. ``force=True``（``--rebuild`` flag）→ 必 rebuild，记录"forced"
    2. ``web/dist/index.html`` 不存在 → 必 rebuild，记录"missing"
    3. ``web/src/**`` 任意源文件（``.ts/.tsx/.css/.html/.json``）的 mtime
       比 dist 新 → 自动 rebuild，记录"stale (newer source: ...)"

    任一条件命中即返回 True。同时 ``click.echo`` 一行说明触发原因，方便
    用户理解为什么 build。

    设计取舍：

    - 不扫 ``node_modules`` / ``dist`` 自身（明显排除）
    - 扫 ``web/package.json`` / ``web/vite.config.ts`` 等构建配置（这些改了
      也应该 rebuild，所以纳入扫描范围）
    - 用 ``Path.rglob`` 而非 ``find`` 子进程，跨平台
    """
    web_dir = _REPO_ROOT / "web"
    dist_index = web_dir / "dist" / "index.html"

    if force:
        click.echo("Frontend rebuild forced (--rebuild)...")
        return True

    if not dist_index.exists():
        click.echo("Frontend dist not found, building...")
        return True

    dist_mtime = dist_index.stat().st_mtime
    # 扫 web/src/** 和 web/ 顶层关键构建配置
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
            if not path.is_file():
                continue
            if path.suffix not in _FRONTEND_SOURCE_EXTENSIONS:
                continue
            # 排除 __tests__（vitest 自管，不入 build）
            if "__tests__" in path.parts:
                continue
            if path.stat().st_mtime > dist_mtime:
                rel = path.relative_to(_REPO_ROOT)
                click.echo(f"Frontend stale (newer source: {rel}), rebuilding...")
                return True

    for cfg in candidate_files:
        if cfg.exists() and cfg.stat().st_mtime > dist_mtime:
            rel = cfg.relative_to(_REPO_ROOT)
            click.echo(f"Frontend stale (newer config: {rel}), rebuilding...")
            return True

    return False


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Web 服务进程管理。"""


@cli.command()
@click.option(
    "--rebuild",
    is_flag=True,
    default=False,
    help="强制重建前端 dist（绕过 mtime 检测；用于源文件改了但 mtime 未触发的场景）。",
)
def start(rebuild: bool) -> None:
    """启动 web 后台进程。

    步骤：

    1. 已有进程在 port 监听 → 报错退出（提示 restart）
    2. 前端 dist 检查（三档触发 rebuild，避免协议陈旧）：
       - ``--rebuild`` 显式 → 必 rebuild
       - ``web/dist/index.html`` 不存在 → 必 rebuild
       - ``web/src/**`` 任意源文件 mtime 比 dist 新 → 自动 rebuild
       否则跳过 build
    3. ``subprocess.Popen + start_new_session=True`` 起 ``web.run``，
       stdout/stderr 重定向到 log 文件
    4. 写 PID 文件
    5. 轮询 10 秒等端口监听上；超时报错并 tail log
    """
    _ensure_dirs()
    progress = StartupProgress(_REPO_ROOT / ".kongming")
    progress.report("env")

    port = _read_port()
    host = _read_host()

    # 1. 端口占用检查
    existing = _find_pid_by_port(port)
    if existing is not None:
        click.echo(f"Already running (PID {existing}, port {port})")
        click.echo("Use 'restart' to restart.")
        return
    progress.report("port")

    # 2. 前端 dist 检查（三档：--rebuild / 不存在 / mtime stale）
    progress.report("frontend")
    if _should_rebuild_dist(force=rebuild):
        try:
            subprocess.run(
                ["npm", "run", "build"],
                cwd=_REPO_ROOT / "web",
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            progress.fail(f"Frontend build failed: {exc}")
            click.echo(f"Frontend build failed: {exc}", err=True)
            sys.exit(1)

    # 3. 起后台进程
    click.echo(f"Starting web server on {host}:{port}...")
    with open(_LOG_FILE, "ab") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "web.run"],
            cwd=_REPO_ROOT,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # 脱离父进程组（等价 nohup）
            env=os.environ,  # 继承 dotenv 加载后的 env（含 KONGMING_WEB_*）
        )

    # 4. 写 PID
    _PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    # 5. 轮询等端口
    for _ in range(10):
        if not _pid_alive(proc.pid):
            progress.fail("Web process died during startup")
            click.echo("Failed to start. Last log lines:", err=True)
            _tail_log(5)
            _PID_FILE.unlink(missing_ok=True)
            sys.exit(1)
        if _is_port_listening(port):
            click.echo(f"Started (PID {proc.pid})")
            click.echo(f"  URL: http://localhost:{port}")
            click.echo(f"  Log: {_LOG_FILE}")
            return
        time.sleep(1)

    click.echo(f"Still starting... check {_LOG_FILE}")


@cli.command()
def stop() -> None:
    """停止 web 后台进程。

    优先按 PID 文件停；失败 fallback 用 lsof 找端口监听者。先 SIGTERM
    + 5s 等死，超时 SIGKILL 强杀。
    """
    port = _read_port()
    pid = _resolve_running_pid(port)
    if pid is None:
        click.echo("Not running")
        _PID_FILE.unlink(missing_ok=True)
        return

    click.echo(f"Stopping (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # 进程刚消失，忽略
        _PID_FILE.unlink(missing_ok=True)
        click.echo("Stopped (already gone)")
        return

    # 等 5 秒
    for _ in range(5):
        if not _pid_alive(pid):
            click.echo("Stopped")
            _PID_FILE.unlink(missing_ok=True)
            return
        time.sleep(1)

    # 强杀
    click.echo("Force killing...")
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    _PID_FILE.unlink(missing_ok=True)
    click.echo("Stopped (force)")


@cli.command()
@click.option(
    "--rebuild",
    is_flag=True,
    default=False,
    help="重启时强制重建前端 dist（透传给 start）。",
)
@click.pass_context
def restart(ctx: click.Context, rebuild: bool) -> None:
    """重启 = stop + 1s + start。``--rebuild`` 透传给 start。"""
    ctx.invoke(stop)
    time.sleep(1)
    ctx.invoke(start, rebuild=rebuild)


@cli.command()
def status() -> None:
    """报当前 web 服务运行状态。"""
    port = _read_port()
    pid = _resolve_running_pid(port)
    if pid is None:
        click.echo("Not running")
        _PID_FILE.unlink(missing_ok=True)
        return
    click.echo(f"Running (PID {pid}, port {port})")
    click.echo(f"  URL: http://localhost:{port}")
    click.echo(f"  Log: {_LOG_FILE}")


@cli.command()
def log() -> None:
    """``tail -f`` 服务日志（阻塞，Ctrl-C 退出）。"""
    if not _LOG_FILE.exists():
        click.echo(f"No log file at {_LOG_FILE}")
        return
    with contextlib.suppress(KeyboardInterrupt):
        subprocess.run(["tail", "-f", str(_LOG_FILE)], check=False)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve_running_pid(port: int) -> int | None:
    """优先按 PID 文件 + 存活校验；失败 fallback 按端口找。"""
    if _PID_FILE.exists():
        try:
            pid = int(_PID_FILE.read_text(encoding="utf-8").strip())
            if _pid_alive(pid):
                return pid
        except (ValueError, OSError):
            pass  # PID 文件损坏 / 进程已死，落到 lsof
    return _find_pid_by_port(port)


def _tail_log(n: int) -> None:
    """打印 log 末尾 n 行（错误诊断用）。"""
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


if __name__ == "__main__":
    cli()
