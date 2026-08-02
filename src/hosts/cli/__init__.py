"""kongming-agent CLI 宿主包。

CLI 是第一个真实宿主产品入口，但不是系统核心。具体运行时装配在
:meth:`runtime_assembly.session_engine.SessionEngine.build`，
交互语义在 :mod:`hosts.cli.interactive_loop` / :mod:`hosts.shared.host_dispatcher`。

依赖方向：``hosts.cli`` 消费 ``hosts.shared / tools / infrastructure.config / core``。

本 ``__init__`` 故意不 ``from hosts.cli.main import main``——那样会让 ``hosts.cli.main``
在 ``import hosts.cli`` 阶段就被加载，随后 ``python -m hosts.cli.main`` 会触发
"found in sys.modules after import of package 'cli', but prior to execution
of 'hosts.cli.main'" 的 RuntimeWarning。需要程序入口时显式 ``from hosts.cli.main import main``。
"""

from __future__ import annotations

from hosts.cli.adapter import CLIAdapter, CLIEventSink
from hosts.cli.stream_sink import CLIStreamSink

__all__ = [
    "CLIAdapter",
    "CLIEventSink",
    "CLIStreamSink",
]
