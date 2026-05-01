"""sidecar 进程入口。

启动方式::

    python -m claude_sidecar

进程启动后立即向 stdout 输出 ``claude_sidecar_ready`` 事件，然后进入 stdin
readline 循环等待 :class:`~claude_sidecar.protocol.SidecarRequest`。

这里**只做** ``asyncio.run`` 桥接 —— 所有逻辑在
:func:`claude_sidecar.main.run_sidecar`，便于测试时直接调而不启子进程。
"""

from __future__ import annotations

import asyncio
import sys

from claude_sidecar.main import run_sidecar


def main() -> int:
    return asyncio.run(run_sidecar())


if __name__ == "__main__":
    sys.exit(main())
