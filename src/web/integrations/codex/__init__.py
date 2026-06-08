"""Codex CLI 接入通道（v0.1）。

与 ``src/web/integrations/claude_code/`` 平级，通过 spawn ``codex exec --json`` 子进程驱动。
公开入口：``router``（FastAPI WebSocket）、``CodexService``、``map_permission_mode``。

详细方案见 ``docs/codex-web-integration-v0.1/README.md``.
"""

from __future__ import annotations

from web.integrations.codex.approval import map_permission_mode
from web.integrations.codex.route import router
from web.integrations.codex.service import CodexService

__all__ = ["CodexService", "map_permission_mode", "router"]
