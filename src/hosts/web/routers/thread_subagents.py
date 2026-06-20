"""Thread 子 agent 生命周期 REST 路由。

功能：提供 ``GET /api/threads/{thread_id}/subagents``，让 Web UI 查询当前
thread 正在工作的子 agent 列表。
作用：把 application.subagents 维护的生命周期记录转换成前端 DTO，默认只返回
running 记录，调试时可通过 include_finished 查看近期结束记录。
关键执行流程：校验 thread id 和 thread metadata，读取 app.state 中的
SubAgentLifecycleRegistry，按 include_finished 过滤记录，再补齐稳定 id 和毫秒
时间字段输出。
关键函数：list_thread_subagents 处理 REST 请求，_item_payload 转换 DTO，
_record_id 生成稳定 id，_iso_to_epoch_ms 把 ISO 时间转成 epoch ms。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol, cast

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from hosts.web.errors import InvalidRequestError, InvalidThreadIdError, ThreadNotFoundError
from hosts.web.routers.threads import THREAD_ID_RE

if TYPE_CHECKING:
    from hosts.web.threads.types import ThreadManagerProtocol

router = APIRouter(prefix="/api/threads", tags=["thread-subagents"])


class _SubAgentLifecycleRecord(Protocol):
    """Router 需要的子 agent 生命周期记录最小协议。"""

    thread_id: str
    source: str
    workflow_id: str | None
    task_id: str
    task_run_id: str
    task_name: str
    session_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    started_at: str
    updated_at: str
    finished_at: str | None
    error_message: str | None


class _SubAgentLifecycleRegistry(Protocol):
    """Router 需要的生命周期 registry 最小协议。"""

    def list_thread(self, thread_id: str, *, limit: int = 50) -> list[_SubAgentLifecycleRecord]:
        """列出 thread 的子 agent 生命周期记录。"""
        ...


class _EmptySubAgentLifecycleRegistry:
    """未注入真实 registry 时的空实现。"""

    def list_thread(self, thread_id: str, *, limit: int = 50) -> list[_SubAgentLifecycleRecord]:
        """返回空记录，输入为 thread id 和数量上限，输出为空列表。"""
        return []


_EMPTY_REGISTRY = _EmptySubAgentLifecycleRegistry()


class ThreadSubAgentItemPayload(BaseModel):
    """单个子 agent 生命周期响应项。

    职责：描述一个子 agent 在当前 thread 下的运行状态和展示时间。
    关键输入：SubAgentLifecycleRecord 以及 router 派生的稳定 id 和毫秒时间。
    关键输出：前端列表可直接消费的严格数据对象。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    thread_id: str
    source: str = Field(max_length=128)
    workflow_id: str | None = Field(default=None, max_length=256)
    task_id: str = Field(max_length=256)
    task_run_id: str = Field(max_length=256)
    task_name: str = Field(max_length=512)
    session_id: str = Field(max_length=512)
    status: Literal["running", "completed", "failed", "cancelled"]
    started_at: str
    updated_at: str
    finished_at: str | None = None
    started_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)
    finished_at_ms: int | None = Field(default=None, ge=0)
    error_message: str | None = Field(default=None, max_length=2000)


class ThreadSubAgentListPayload(BaseModel):
    """当前 thread 子 agent 列表响应体。

    职责：承载 schema_version、thread_id 和子 agent 响应项列表。
    关键输入：list_thread_subagents 过滤后的生命周期记录集合。
    关键输出：GET /api/threads/{thread_id}/subagents 的响应体。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    thread_id: str
    subagents: list[ThreadSubAgentItemPayload]


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


def _registry(request: Request) -> _SubAgentLifecycleRegistry:
    """读取生命周期 registry，输入为 Request，输出为已配置或默认 registry。"""
    registry = getattr(request.app.state, "subagent_lifecycle_registry", None)
    if registry is None:
        return _EMPTY_REGISTRY
    if not callable(getattr(registry, "list_thread", None)):
        raise InvalidRequestError("subagent lifecycle registry is invalid", status_code=500)
    return cast(_SubAgentLifecycleRegistry, registry)


def _record_id(record: _SubAgentLifecycleRecord) -> str:
    """生成 DTO id，输入为生命周期记录，输出为来源和运行坐标组成的稳定 id。"""
    workflow = record.workflow_id or "-"
    return "|".join(
        [
            f"source:{record.source}",
            f"workflow:{workflow}",
            f"task_run:{record.task_run_id}",
            f"session:{record.session_id}",
        ]
    )


def _iso_to_epoch_ms(value: str) -> int:
    """转换 ISO 时间，输入为 ISO 8601 字符串，输出为 epoch 毫秒。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _optional_iso_to_epoch_ms(value: str | None) -> int | None:
    """转换可选 ISO 时间，输入为字符串或 None，输出为 epoch 毫秒或 None。"""
    if value is None:
        return None
    return _iso_to_epoch_ms(value)


def _item_payload(record: _SubAgentLifecycleRecord) -> ThreadSubAgentItemPayload:
    """转换 REST item，输入为生命周期记录，输出为前端消费 DTO。"""
    return ThreadSubAgentItemPayload(
        id=_record_id(record),
        thread_id=record.thread_id,
        source=record.source,
        workflow_id=record.workflow_id,
        task_id=record.task_id,
        task_run_id=record.task_run_id,
        task_name=record.task_name,
        session_id=record.session_id,
        status=record.status,
        started_at=record.started_at,
        updated_at=record.updated_at,
        finished_at=record.finished_at,
        error_message=record.error_message,
        started_at_ms=_iso_to_epoch_ms(record.started_at),
        updated_at_ms=_iso_to_epoch_ms(record.updated_at),
        finished_at_ms=_optional_iso_to_epoch_ms(record.finished_at),
    )


@router.get("/{thread_id}/subagents")
async def list_thread_subagents(
    thread_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    include_finished: bool = Query(default=False),
) -> ThreadSubAgentListPayload:
    await _require_thread(request, thread_id)
    records = await asyncio.to_thread(_registry(request).list_thread, thread_id, limit=limit)
    if not include_finished:
        records = [record for record in records if record.status == "running"]
    return ThreadSubAgentListPayload(
        schema_version=1,
        thread_id=thread_id,
        subagents=[_item_payload(record) for record in records],
    )


__all__ = [
    "ThreadSubAgentItemPayload",
    "ThreadSubAgentListPayload",
    "router",
]
