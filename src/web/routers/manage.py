"""管理页路由（v0.1.5）。

端点：

- ``GET  /api/manage/cells``                — 列出所有活的 cell
- ``POST /api/manage/cells/{thread_id}/stop`` — 手动停止单个 cell（204）

注：v0.1.5 管理页用 REST polling，**没有** ``WS /ws/manage`` 订阅；后续 v0.1.6+ 再加。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from web.errors import InvalidThreadIdError, ThreadNotFoundError
from web.protocol import CellSummaryDTO

if TYPE_CHECKING:
    from web.types import ThreadManagerProtocol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/manage", tags=["manage"])

THREAD_ID_RE: re.Pattern[str] = re.compile(r"^thread-[a-f0-9]{12}$")


def _validate_thread_id(thread_id: str) -> None:
    if not THREAD_ID_RE.match(thread_id):
        raise InvalidThreadIdError(
            f"invalid thread_id: {thread_id!r}; must match ^thread-[a-f0-9]{{12}}$"
        )


@router.get("/cells")
async def list_cells(request: Request) -> list[CellSummaryDTO]:
    """列出所有活的 cell。"""
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    return tm.list_cells()


@router.post("/cells/{thread_id}/stop", status_code=204)
async def stop_cell(thread_id: str, request: Request) -> None:
    """手动停止单个 cell。

    语义：

    - 不存在的 cell → 404 :class:`ThreadNotFoundError`
    - 已停的（``ThreadManager.evict_cell`` 幂等）→ 仍 204
    """
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    if tm.get_cell(thread_id) is None:
        raise ThreadNotFoundError(f"cell not found: {thread_id}")
    await tm.evict_cell(thread_id, reason="manual_stop", notify_ws=True)


__all__ = ["router"]
