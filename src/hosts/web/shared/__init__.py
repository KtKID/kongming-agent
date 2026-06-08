"""Web 子模块共享层（v0.1）。

存放跨 ``claude_code`` / ``codex`` / 未来 ``gemini`` 等 provider 的协议中立组件。

当前内容：

- :mod:`session_manager` — 活跃 session 表（含 writer / abort_event / query_task）
"""

from __future__ import annotations
