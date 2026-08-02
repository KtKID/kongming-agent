"""Thread 子 agent TaskRegistry 投影 REST 路由。

功能：提供 ``GET /api/threads/{thread_id}/subagents``，让 Web UI 查询当前
thread 正在工作的子 agent 列表。
作用：通过 ThreadManager → ThreadCell → HostDispatcher → AgentManager 读取同一份
TaskRegistry 投影，默认只返回 live 记录，调试时可通过 include_finished 查看近期终态。
关键执行流程：校验 thread id 和 metadata，读取已 boot cell 的 dispatcher 任务快照，
把 epoch 毫秒转成 REST 时间字段。
关键函数：list_thread_subagents 处理 REST 请求，_task_records 读取 owner 投影，
_item_payload 转换 DTO。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from fastapi import APIRouter, Query, Request

from hosts.web.errors import InvalidRequestError, InvalidThreadIdError, ThreadNotFoundError
from hosts.web.protocol import ThreadSubAgentItemDTO, ThreadSubAgentListDTO
from hosts.web.routers.threads import THREAD_ID_RE

if TYPE_CHECKING:
    from hosts.web.threads.types import ThreadManagerProtocol

router = APIRouter(prefix="/api/threads", tags=["thread-subagents"])


class _TaskProjection(Protocol):
    """Router 需要的 TaskRegistry 不可变投影最小协议。"""

    agent_id: str
    thread_id: str
    source: str
    workflow_id: str | None
    workflow_task_id: str | None
    task_id: str
    task_run_id: str
    task_name: str
    session_id: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    started_at: int
    updated_at: int
    finished_at: int | None
    error_message: str | None


def _validate_thread_id(thread_id: str) -> None:
    """校验 thread id，输入为路径参数，输出为空或抛 InvalidThreadIdError。"""
    if not THREAD_ID_RE.match(thread_id):
        raise InvalidThreadIdError(
            f"invalid thread_id: {thread_id!r}; must match ^thread-[a-f0-9]{{12}}$"
        )


async def _require_thread(request: Request, thread_id: str) -> None:
    """确认 thread 存在，输入为 Request 和 thread id，输出为空或抛 404。"""
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    metas = await asyncio.to_thread(tm.list_threads)
    if not any(meta.id == thread_id for meta in metas):
        raise ThreadNotFoundError(f"thread not found: {thread_id}")


def _task_records(
    request: Request,
    thread_id: str,
    *,
    include_finished: bool,
    limit: int,
) -> tuple[_TaskProjection, ...]:
    """经 ThreadManager/HostDispatcher 读取任务快照，未 boot thread 返回空元组。"""
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    cell = tm.get_cell(thread_id)
    if cell is None:
        return ()
    dispatcher = cell.host_dispatcher
    if getattr(dispatcher, "agent_manager", None) is None:
        return ()
    list_records = getattr(dispatcher, "list_task_records", None)
    if not callable(list_records):
        raise InvalidRequestError("thread host dispatcher is invalid", status_code=500)
    records = list_records(include_finished=include_finished, limit=limit)
    return tuple(records)


def _epoch_ms_to_iso(value: int) -> str:
    """转换 epoch 毫秒为 UTC ISO 时间。"""
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _optional_epoch_ms_to_iso(value: int | None) -> str | None:
    """转换可选 epoch 毫秒为 UTC ISO 时间。"""
    if value is None:
        return None
    return _epoch_ms_to_iso(value)


def _item_payload(record: _TaskProjection) -> ThreadSubAgentItemDTO:
    """转换 REST item，输入为 TaskRegistry 投影，输出为前端消费 DTO。"""
    return ThreadSubAgentItemDTO(
        id=record.task_id,
        agent_id=record.agent_id,
        thread_id=record.thread_id,
        source=record.source,
        workflow_id=record.workflow_id,
        workflow_task_id=record.workflow_task_id,
        task_id=record.task_id,
        task_run_id=record.task_run_id,
        task_name=record.task_name,
        session_id=record.session_id,
        status=record.status,
        started_at=_epoch_ms_to_iso(record.started_at),
        updated_at=_epoch_ms_to_iso(record.updated_at),
        finished_at=_optional_epoch_ms_to_iso(record.finished_at),
        error_message=record.error_message,
        started_at_ms=record.started_at,
        updated_at_ms=record.updated_at,
        finished_at_ms=record.finished_at,
    )


@router.get("/{thread_id}/subagents")
async def list_thread_subagents(
    thread_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    include_finished: bool = Query(default=False),
) -> ThreadSubAgentListDTO:
    await _require_thread(request, thread_id)
    records = await asyncio.to_thread(
        _task_records,
        request,
        thread_id,
        include_finished=include_finished,
        limit=limit,
    )
    return ThreadSubAgentListDTO(
        schema_version=1,
        thread_id=thread_id,
        subagents=[_item_payload(record) for record in records],
    )


__all__ = [
    "router",
]
