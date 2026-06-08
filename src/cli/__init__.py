"""kongming-agent CLI 包。

CLI 是第一个真实宿主产品入口，但不是系统核心。具体运行时装配在
:meth:`runtime_assembly.native_runtime.NativeRuntime.build`，
交互语义在 :mod:`host.cli_adapter` / :mod:`host.session_bridge`。

依赖方向：``cli/`` 消费 ``host/ / tools/ / executors/ / infrastructure.config / core``，
反向不成立。

本 ``__init__`` 故意不 ``from cli.main import main``——那样会让 ``cli.main``
在 ``import cli`` 阶段就被加载，随后 ``python -m cli.main`` 会触发
"found in sys.modules after import of package 'cli', but prior to execution
of 'cli.main'" 的 RuntimeWarning。需要程序入口时显式 ``from cli.main import main``。
"""

from __future__ import annotations

__all__: list[str] = []
