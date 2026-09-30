"""轨迹时间线路由（track-timeline-v0.1 · task-trajectory-backend）。

``GET /api/threads/{thread_id}/trajectory``：返回 thread 的只读轨迹投影
（真源 = ``<session_root>/<thread_id>/trace.jsonl``）。

错误语义：

- 422：thread_id 不匹配 ``^thread-[a-f0-9]{12}$``
- 404：thread 不存在（经 ``ThreadManager.list_threads`` 判定）
- 200 + 空 runs/records：thread 存在但从未产生轨迹（不是错误）

投影与文件定位全部委托 :class:`hosts.web.trajectory.TrajectoryManager`
（模块间只调门户）；本路由只做参数校验与存在性判定。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from hosts.web.errors import InvalidThreadIdError, ThreadNotFoundError
from hosts.web.protocol import TrajectoryResponseDTO
from hosts.web.routers.threads import THREAD_ID_RE
from hosts.web.trajectory import TrajectoryManager

router = APIRouter(prefix="/api/threads", tags=["thread-trajectory"])


@router.get("/{thread_id}/trajectory", response_model=TrajectoryResponseDTO)
async def get_thread_trajectory(thread_id: str, request: Request) -> TrajectoryResponseDTO:
    """返回 thread 的轨迹时间线投影（runs 分段 + records 账本 + 总览统计）。"""
    if not THREAD_ID_RE.match(thread_id):
        raise InvalidThreadIdError(
            f"invalid thread_id: {thread_id!r}; must match ^thread-[a-f0-9]{{12}}$"
        )
    tm = request.app.state.thread_manager
    metas = await asyncio.to_thread(tm.list_threads)
    if not any(meta.id == thread_id for meta in metas):
        raise ThreadNotFoundError(f"thread not found: {thread_id}")
    trajectory_manager: TrajectoryManager = request.app.state.trajectory_manager
    return await trajectory_manager.get_trajectory(thread_id)
