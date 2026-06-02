"""Detached subprocess 触发 ``./start.sh web restart``。

manage-config-tab dev-checklist #4 产物。

设计要点：

- **为什么 detached（``start_new_session=True``）**：
  ``./start.sh web restart`` 内部会 kill 当前 web server 进程并拉起新的 web
  server。如果子进程跟父进程在同一个 process group / session，父进程被 kill
  时（包括 SIGTERM / 整个 session 被收掉），子进程也会被一起带走，导致重启
  脚本跑到一半被掐断。``start_new_session=True`` 等价于 ``setsid``，把子进程
  放到新的 session + process group，跟当前 web server 彻底脱钩。
  选这个 kwarg 而不是 ``preexec_fn=os.setsid``：前者 cross-platform、
  ruff 不报 B606，行为更可预测。

- **为什么 stdin/stdout/stderr 全部 ``DEVNULL``**：
  父进程（FastAPI / uvicorn）即将被 kill，它持有的 stdin/stdout/stderr
  fd 在 kill 后会关闭。子进程如果继承这些 fd 并尝试写日志，会触发
  ``BrokenPipeError`` 直接崩。``DEVNULL`` 让子进程的标准流指向 ``/dev/null``，
  跟父进程的 fd 完全无关。stdin 接 ``DEVNULL`` 还能避免 Claude Code Bash 工具
  PreToolUse hook 残留 stdin 之类的诡异问题。

- **为什么这里不 sleep**：
  README 里写的"先 sleep 0.5s 让 HTTP 200 响应发出"是说**调用方**（FastAPI
  router 那一层）应该处理响应顺序，不是本模块的责任。本模块只负责把子进程
  spawn 出去就立即返回，让上层有机会先把 HTTP 响应 flush 给浏览器、再让重启
  脚本去 kill 自己。在这里 sleep 会让 endpoint 多 hold 0.5s，违反"装配与执行
  分离"。延迟由 ``start.sh`` 内部脚本逻辑或调用方编排，本模块不染指。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class RestartScriptNotFoundError(Exception):
    """``./start.sh`` 不存在或不可执行。"""


_START_SCRIPT_NAME = "start.sh"


def find_start_script(repo_root: Path) -> Path:
    """定位 ``./start.sh``。

    Args:
        repo_root: 仓库根目录（应当包含 ``start.sh``）。

    Returns:
        ``start.sh`` 的绝对路径。

    Raises:
        RestartScriptNotFoundError: 文件不存在或缺少可执行位（``os.X_OK``）。
    """
    script = repo_root / _START_SCRIPT_NAME
    if not script.is_file():
        raise RestartScriptNotFoundError(f"start script not found: {script}")
    if not os.access(script, os.X_OK):
        raise RestartScriptNotFoundError(f"start script not executable (missing +x): {script}")
    return script


def trigger_restart(repo_root: Path) -> int:
    """Spawn detached ``./start.sh web restart``，立即返回子进程 pid。

    子进程脱离当前 session（``start_new_session=True``），父进程被 kill 时
    不会被一起带走。三个标准流全部接 ``DEVNULL``，避免父 fd 关闭后子进程
    写日志触发 ``BrokenPipeError``。

    本函数 **不 wait**、**不 sleep**、**不 poll**——返回 pid 仅供调试 / 日志，
    调用方不应依赖该 pid 判断重启成功。

    Args:
        repo_root: 仓库根目录，必须包含可执行的 ``start.sh``。

    Returns:
        子进程的 pid。

    Raises:
        RestartScriptNotFoundError: ``./start.sh`` 不存在或不可执行。
    """
    script = find_start_script(repo_root)
    proc = subprocess.Popen(
        ["nohup", str(script), "web", "restart"],
        cwd=str(repo_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid
