"""Kongming Web 后端 sidecar 的 PyInstaller 入口。

功能：
    把 PyInstaller 生成的可执行文件入口收口到 `hosts.web.run.main`。

作用：
    PyInstaller 更适合以脚本文件作为入口。本文件只做转发，避免构建脚本
    依赖 console script 安装状态。

关键执行流程：
    1. 导入 `hosts.web.run.main`。
    2. 把命令行参数交给 Web 宿主入口解析。
    3. 使用入口返回值作为进程退出码。

关键函数：
    `main`：执行入口转发，输入为系统命令行参数，输出为进程退出码。
"""

from __future__ import annotations

import sys

from hosts.web.run import main as run_web


def main() -> int:
    """执行 Web sidecar 入口转发。

    Returns:
        `hosts.web.run.main` 返回的进程退出码。
    """
    return run_web()


if __name__ == "__main__":
    sys.exit(main())
