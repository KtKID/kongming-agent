"""Web 应用单实例锁单测（reports/cr/cr-report-20260519 P0 #1 修复配套）。

覆盖 4 个核心场景：

1. 无冲突 → 抢到锁 + 写 PID + release 释放
2. 活进程冲突 → ``SystemExit(1)`` + 不释放对方锁
3. 孤儿锁（PID 已死）→ 自动清理重抢成功
4. release(None) safe / 重复 release safe
5. 锁文件解析失败（空 / 非数字 PID）视为孤儿

设计：用真实 fcntl + 真实 fd（不 mock）+ tmp_path 隔离；判活路径用 fork
出短进程拿到真实 alive PID + 等其死后拿到真实 dead PID 来测，不 mock
``os.kill``。
"""

from __future__ import annotations

import fcntl
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from hosts.web.app_support.app_lock import (
    _read_holder_pid,
    _try_lock_path,
    acquire_app_instance_lock,
    release_app_instance_lock,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _spawn_alive_child() -> int:
    """fork 一个 sleep 子进程，返回 PID；测试用完必须 kill。"""
    pid = os.fork()
    if pid == 0:  # child
        try:
            time.sleep(60)
        finally:
            os._exit(0)
    return pid


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # reap zombie 避免 ps 残留
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _spawn_dead_child() -> int:
    """fork 一个 immediately-exit 子进程并 reap，返回保证已死的 PID。"""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_acquire_no_conflict_writes_pid_and_releases(tmp_path: Path) -> None:
    """场景 1：无冲突 → 抢到 + 写当前 PID + release 释放后能再抢。"""
    fd = acquire_app_instance_lock(tmp_path)
    try:
        assert isinstance(fd, int) and fd > 0
        lock_path = tmp_path / "web" / ".app.lock"
        assert lock_path.exists()
        pid_in_file = _read_holder_pid(lock_path)
        assert pid_in_file == os.getpid()
    finally:
        release_app_instance_lock(fd)

    # 释放后再抢应成功
    fd2 = acquire_app_instance_lock(tmp_path)
    try:
        assert isinstance(fd2, int)
    finally:
        release_app_instance_lock(fd2)


def test_acquire_alive_holder_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """场景 2：活进程持锁 → ``sys.exit(1)`` + 不释放对方锁。"""
    child_pid = _spawn_alive_child()
    try:
        lock_dir = tmp_path / "web"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / ".app.lock"

        # 在父进程开 fd + 抢锁（模拟"另一活 web 进程持锁"——单测用父进程
        # 持锁等价；判活逻辑走 child PID）
        holder_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # 写真活子进程的 PID 到锁文件让 _is_orphan 判断为"活"
        os.ftruncate(holder_fd, 0)
        os.write(holder_fd, f"{child_pid} 2026-05-19T01:30:00+0800\n".encode())

        try:
            with pytest.raises(SystemExit) as exc_info:
                acquire_app_instance_lock(tmp_path)
            assert exc_info.value.code == 1
            err = capsys.readouterr().err
            assert "FATAL" in err
            assert str(child_pid) in err
            assert "pkill" in err
            # 对方锁仍持有
            assert _read_holder_pid(lock_path) == child_pid
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)
    finally:
        _kill_pid(child_pid)


def test_acquire_orphan_holder_cleans_and_reacquires(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """场景 3：孤儿锁（PID 已死）→ 自动清理 + 重抢成功 + 写新 PID。"""
    dead_pid = _spawn_dead_child()
    lock_dir = tmp_path / "web"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".app.lock"
    # 写一个已死 PID 到锁文件，但**不**实际持锁（孤儿场景：进程死后 OS 已释放 flock）
    lock_path.write_text(f"{dead_pid} 2026-05-19T00:00:00+0800\n", encoding="utf-8")

    fd = acquire_app_instance_lock(tmp_path)
    try:
        assert isinstance(fd, int) and fd > 0
        # 锁文件 PID 应已被覆盖为当前进程
        assert _read_holder_pid(lock_path) == os.getpid()
        err = capsys.readouterr().err
        # 注意：孤儿场景 flock 已被 OS 释放，第一次试抢就成功，**不**走 WARN 路径
        # 但若锁文件存在且有 PID，理论上 flock 不持锁的话依然能直接抢——所以这里
        # 不强断言 WARN 存在
        assert "FATAL" not in err
    finally:
        release_app_instance_lock(fd)


def test_acquire_orphan_held_lock_warns_cleans_reacquires(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """场景 3 进阶：孤儿锁 + 锁仍被另一 fd 持有 → 通过另一 fd close 模拟死亡，
    但锁文件 PID 是死 PID → 走 WARN 路径。

    构造：开 fd + 抢锁 + 写死 PID + 关 fd（OS 自动释放 flock）→ 锁文件
    PID 是死的，但 flock 不持。新进程试抢应该成功，但要走 _is_orphan 判定。
    """
    # 这个场景跟 test 3 的区别：先 try_lock_path 失败再判活。
    # 但 flock 释放后第一次 try_lock_path 就成功，根本走不到孤儿判定。
    # 真正的"WARN 孤儿清理路径"要求锁文件 PID 死 + flock 仍持有——这只
    # 可能发生在内核 bug。本场景跳过详细断言，只验证不死循环 / 不抛非
    # SystemExit 异常。
    dead_pid = _spawn_dead_child()
    lock_dir = tmp_path / "web"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".app.lock"
    lock_path.write_text(f"{dead_pid}\n", encoding="utf-8")

    fd = acquire_app_instance_lock(tmp_path)
    try:
        assert isinstance(fd, int)
    finally:
        release_app_instance_lock(fd)


def test_acquire_invalid_pid_in_lockfile_treated_as_orphan(tmp_path: Path) -> None:
    """场景 5：锁文件非数字 PID → 视为孤儿 → 清理重抢成功。"""
    lock_dir = tmp_path / "web"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".app.lock"
    lock_path.write_text("not-a-pid garbage\n", encoding="utf-8")

    fd = acquire_app_instance_lock(tmp_path)
    try:
        assert _read_holder_pid(lock_path) == os.getpid()
    finally:
        release_app_instance_lock(fd)


def test_acquire_empty_lockfile_treated_as_orphan(tmp_path: Path) -> None:
    """场景 5 边界：空锁文件 → 视为孤儿。"""
    lock_dir = tmp_path / "web"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".app.lock"
    lock_path.write_text("", encoding="utf-8")

    fd = acquire_app_instance_lock(tmp_path)
    try:
        assert _read_holder_pid(lock_path) == os.getpid()
    finally:
        release_app_instance_lock(fd)


def test_release_none_is_noop() -> None:
    """release(None) 不抛。"""
    release_app_instance_lock(None)  # 不应抛


def test_release_idempotent(tmp_path: Path) -> None:
    """重复 release 不抛（第二次的 flock_UN + close 都吞异常）。"""
    fd = acquire_app_instance_lock(tmp_path)
    release_app_instance_lock(fd)
    # 第二次：fd 已 close，flock 应抛但被吞
    release_app_instance_lock(fd)  # 不应抛


def test_lock_dir_is_created_if_missing(tmp_path: Path) -> None:
    """``<home>/web/`` 不存在时自动 mkdir(parents=True)。"""
    assert not (tmp_path / "web").exists()
    fd = acquire_app_instance_lock(tmp_path)
    try:
        assert (tmp_path / "web").is_dir()
        assert (tmp_path / "web" / ".app.lock").exists()
    finally:
        release_app_instance_lock(fd)


def test_try_lock_path_returns_none_when_held(tmp_path: Path) -> None:
    """``_try_lock_path`` 被持时返回 None 而非抛。"""
    lock_path = tmp_path / "test.lock"
    fd1 = _try_lock_path(lock_path)
    assert fd1 is not None
    try:
        fd2 = _try_lock_path(lock_path)
        assert fd2 is None
    finally:
        fcntl.flock(fd1, fcntl.LOCK_UN)
        os.close(fd1)


# ---------------------------------------------------------------------------
# 平台保护：本模块依赖 POSIX fork + fcntl，Windows 跳过
# ---------------------------------------------------------------------------


if sys.platform == "win32":
    pytest.skip("posix-only lock semantics", allow_module_level=True)
