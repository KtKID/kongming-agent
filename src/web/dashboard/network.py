"""dashboard 连接计数 getter 集合。"""

from __future__ import annotations

from typing import Any

from web.websocket.cron import get_broker
from web.websocket.thread_status import get_broadcaster


def get_thread_status_connections() -> int:
    return get_broadcaster().connection_count


def get_cron_connections() -> int:
    return get_broker().connection_count


def get_approval_subscribers(inbox_broadcaster: Any) -> int:
    return int(getattr(inbox_broadcaster, "subscriber_count", 0) or 0)


def get_approval_pending(inbox_broadcaster: Any) -> int:
    return int(getattr(inbox_broadcaster, "pending_count", 0) or 0)


def get_cell_chat_ws_connections(cell: Any) -> int:
    adapter = getattr(cell, "adapter", None)
    fanout = getattr(adapter, "_ws", None)
    return int(getattr(fanout, "client_count", 0) or 0)


def get_active_session_count(session_manager: Any) -> int:
    list_active = getattr(session_manager, "list_active", None)
    if not callable(list_active):
        return 0
    return len(list_active())
