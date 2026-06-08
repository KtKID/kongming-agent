"""Web 应用单实例锁（v0.1）。

只在 :mod:`web.run` ``main()`` 启动时调一次。设计目的：

- **防止多个 web server 实例同时跑**——多个 ticker loop 抢同一把
  ``.kongming/cron/.scheduler.lock`` → ``SchedulerBusyError`` 死循环 +
  log 暴涨（5/18 实际观测：30 min 内 server.log 涨到 29 MB / 45 万行）
- **孤儿锁自动清理**：持锁进程已死（``os.kill(pid, 0)`` 抛
  ``ProcessLookupError``）→ 删 lock 重抢一次
- **活进程冲突 fail-fast**：立即 ``sys.exit(1)`` + 打印诊断信息，不让
  ticker loop 静默死循环

不动 ``src/scheduler/`` 任何代码；scheduler 自己的非阻塞 lock + retry
设计针对"短暂并发"是对的，本模块负责消除"长期并存"这个真正病因。

锁文件：``<kongming_home>/web/.app.lock``
锁文件内容：``<pid> <ISO8601 timestamp>\n``（best-effort，便于 ps 排查）

设计权衡：

- 用 ``fcntl.flock`` 而非 ``fcntl.lockf``——前者绑 fd 跨 fork 后子进程
  共享锁状态，但 uvicorn 不 fork（默认单 worker），无差异；选 ``flock``
  是因为更广泛的 POSIX 支持
- 持锁 fd 由 ``main()`` 全程持有；进程退出 OS 自动释放——
  ``release_app_instance_lock`` 是 finally 兜底，不依赖它正确释放
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

_LOCK_FILENAME = ".app.lock"
_LOCK_SUBDIR = "web"
_IS_WIN = sys.platform == "win32"

if not _IS_WIN:
    import fcntl


def acquire_app_instance_lock(home: Path) -> int:
    """启动时抢 web app 实例锁；返回必须长持有的 fd。

    单实例独占语义：

    1. 试抢锁——成功 → 写 ``<pid> <iso ts>\\n`` + 返回 fd
    2. 抢不到 → 读锁文件 PID + ``os.kill(pid, 0)`` 探活：
       - **活进程** → 打印诊断 + ``sys.exit(1)``（不让 ticker 死循环）
       - **孤儿/无效 PID** → 删 lock 重抢一次；重抢仍失败 ``sys.exit(1)``
         （防双启动 race）

    Windows 平台无 ``fcntl``，退化为 PID 文件 + 探活（无文件锁互斥，
    但 dev 环境单进程场景足够）。

    Args:
        home: :func:`infrastructure.config.paths.get_kongming_home` 返回的 home 目录

    Returns:
        持锁的 fd——``main()`` **必须持到进程退出**，否则锁立即释放。
        **不要 close 这个 fd**，传给 :func:`release_app_instance_lock`
        在 finally 释放即可。

    Raises:
        SystemExit: 检测到活进程冲突或 race 抢不到时退出码 1。
    """
    lock_dir = home / _LOCK_SUBDIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / _LOCK_FILENAME

    if _IS_WIN:
        return _acquire_win(lock_path)

    # POSIX: fcntl.flock
    fd = _try_lock_path(lock_path)
    if fd is not None:
        _write_owner(fd)
        return fd

    holder_pid = _read_holder_pid(lock_path)
    if not _is_orphan(holder_pid):
        sys.stderr.write(
            f"[web] FATAL: web app 实例已在运行，PID={holder_pid}\n"
            f"  锁路径: {lock_path}\n"
            f"  本地工具默认单实例。先停掉旧进程：\n"
            f"    kill {holder_pid}    # 或 pkill -f 'src.web'\n"
            f"  若旧进程已不响应：rm {lock_path} 后重试。\n"
        )
        sys.exit(1)

    sys.stderr.write(
        f"[web] WARN: 检测到孤儿锁（PID={holder_pid} 已死），清理后重抢\n  锁路径: {lock_path}\n"
    )
    with contextlib.suppress(FileNotFoundError):
        lock_path.unlink()

    fd = _try_lock_path(lock_path)
    if fd is None:
        sys.stderr.write(
            f"[web] FATAL: 孤儿锁清理后仍抢不到（与另一并行启动 race？）\n  锁路径: {lock_path}\n"
        )
        sys.exit(1)
    _write_owner(fd)
    return fd


def _acquire_win(lock_path: Path) -> int:
    """Windows 退化实现：PID 文件 + 探活，无文件锁。"""
    holder_pid = _read_holder_pid(lock_path)
    if holder_pid is not None and not _is_orphan(holder_pid):
        sys.stderr.write(
            f"[web] FATAL: web app 实例已在运行，PID={holder_pid}\n"
            f"  锁路径: {lock_path}\n"
            f"  若旧进程已不响应，删除锁文件后重试：del {lock_path}\n"
        )
        sys.exit(1)

    if holder_pid is not None:
        sys.stderr.write(f"[web] WARN: 检测到孤儿锁（PID={holder_pid} 已死），清理后重写\n")

    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    _write_owner(fd)
    return fd


def release_app_instance_lock(fd: int | None) -> None:
    """释放锁 + 关 fd；``main()`` finally 兜底用。

    正常退出 OS 也会自动释放——本函数只是显式 best-effort。
    传 ``None`` 等价 no-op；任何异常都吞（退出阶段不该再抛）。
    """
    if fd is None:
        return
    if not _IS_WIN:
        with contextlib.suppress(Exception):
            fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(Exception):
        os.close(fd)


# ---------------------------------------------------------------------------
# 私有辅助
# ---------------------------------------------------------------------------


def _try_lock_path(lock_path: Path) -> int | None:
    """打开 + 试抢 ``LOCK_EX | LOCK_NB``；成功返回 fd，被占返回 None。

    其他 OSError（如权限/磁盘）直接抛——这是装配期问题，让 ``main()``
    层用栈展示具体原因，不要吞。
    """
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except Exception:
        os.close(fd)
        raise
    return fd


def _write_owner(fd: int) -> None:
    """写 ``<pid> <iso ts>\\n`` 进锁文件（best-effort，写失败不影响互斥）。"""
    with contextlib.suppress(Exception):
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        os.write(fd, f"{os.getpid()} {ts}\n".encode())


def _read_holder_pid(lock_path: Path) -> int | None:
    """读锁文件第一段当 PID；解析失败 / 文件缺失返回 None（视为孤儿）。"""
    try:
        text = lock_path.read_text(encoding="utf-8")
    except OSError:
        return None
    parts = text.split()
    if not parts or not parts[0].isdigit():
        return None
    return int(parts[0])


def _is_orphan(pid: int | None) -> bool:
    """PID 无效 / 进程已死 → 孤儿；进程存在 → 活。

    使用 ``os.kill(pid, 0)`` 探测——信号 ``0`` 不实际发送，只触发存在性
    检查。:class:`PermissionError` 表示进程存在但不属于当前 user，**保
    守视为活**，避免误杀同主机其他用户的进程。
    """
    if pid is None or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        # Windows 对无效 PID 抛 OSError(WinError 87) 而非 ProcessLookupError
        return True
    return False


__all__ = [
    "acquire_app_instance_lock",
    "release_app_instance_lock",
]
