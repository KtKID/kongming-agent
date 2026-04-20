"""executors：把 agent 跑起来的执行落地层。

职责划分：

- :mod:`executors.llm`：provider 实现（OpenAI-compatible 等）。
- :mod:`executors.agent_runtime`：运行时装配层，把 provider / tools /
  session / event sinks / 安全链装起来，再调 :class:`core.runner.Runner`。

executors 只依赖 ``core`` 协议和 ``config_loader``；具体 tools / safety /
observability / host / cli 实现通过装配层注入，不在这里 import。
"""

from __future__ import annotations
