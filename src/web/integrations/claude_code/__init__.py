"""Claude Code WebSocket 接入模块（v0.1）。

通过 ``claude_agent_sdk`` 同进程调起 Claude Code，跟现有 generic_chat
（OpenAI/Anthropic provider）平级共存于 ``src/web/`` 主线。

公共入口：

- :data:`router` — FastAPI ``APIRouter``，由 ``web.app.create_app`` 挂载
- :class:`SessionManager` — 全局活跃会话表，挂在 ``app.state.claude_session_manager``
"""

from __future__ import annotations

from web.integrations.claude_code.route import router
from web.shared.session_manager import SessionManager, SessionRecord

__all__ = ["SessionManager", "SessionRecord", "router"]
