"""executors.agent_runtime：进程内运行时装配层。

只暴露 :class:`NativeRuntime`。它负责把 provider / tools / session /
event sinks / 安全链装起来，再调用 :class:`core.runner.Runner`；
**不**自持第二份 turn loop / RunState。
"""

from __future__ import annotations

from executors.agent_runtime.native_runtime import NativeRuntime

__all__ = ["NativeRuntime"]
