"""Thread 任务进度 REST router。

本脚本把当前 thread 的 task_progress.json 暴露给 Web API。
关键流程：校验 thread id 和 metadata，读取 app.state 注入的 task progress service，读取或写入当前 session 快照。
关键函数：get_thread_task_progress 读取快照，put_thread_task_progress 写入受控测试快照，_to_payload 输出 REST DTO。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hosts.web.errors import InvalidRequestError, InvalidThreadIdError, ThreadNotFoundError
from hosts.web.routers.threads import THREAD_ID_RE

if TYPE_CHECKING:
    from hosts.web.threads.metadata import ThreadMetadata
    from hosts.web.threads.types import ThreadManagerProtocol

router = APIRouter(prefix="/api/threads", tags=["thread-task-progress"])

TASK_PROGRESS_MAX_ITEMS = 128
TASK_PROGRESS_MAX_ID_LENGTH = 256
TASK_PROGRESS_MAX_DESC_LENGTH = 1000
TASK_PROGRESS_MAX_ERROR_LENGTH = 2000


class TaskProgressItemPayload(BaseModel):
    """REST task progress item payload."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    orchestration_task_id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    workflow_id: str | None = Field(default=None, max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    task_id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    task_run_id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    desc: str = Field(max_length=TASK_PROGRESS_MAX_DESC_LENGTH)
    status: Literal["pending", "in_progress", "completed"]
    source_status: str | None = Field(default=None, max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    error_message: str | None = Field(default=None, max_length=TASK_PROGRESS_MAX_ERROR_LENGTH)
    display_order: int = Field(ge=0)
    updated_at_ms: int | None = Field(default=None, ge=0)


class PutTaskProgressRequest(BaseModel):
    """PUT body；source 和 session_id 由后端固定。"""

    model_config = ConfigDict(extra="forbid")

    tasks: list[TaskProgressItemPayload] = Field(max_length=TASK_PROGRESS_MAX_ITEMS)


class TaskProgressCountsPayload(BaseModel):
    """REST counts payload."""

    model_config = ConfigDict(extra="forbid")

    pending: int
    in_progress: int
    completed: int
    total: int


class TaskProgressSnapshotPayload(BaseModel):
    """REST snapshot payload."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    session_id: str
    updated_at_ms: int
    source: Literal["api", "llm", "workflow"]
    tasks: list[TaskProgressItemPayload]
    counts: TaskProgressCountsPayload


def _validate_thread_id(thread_id: str) -> None:
    if not THREAD_ID_RE.match(thread_id):
        raise InvalidThreadIdError(
            f"invalid thread_id: {thread_id!r}; must match ^thread-[a-f0-9]{{12}}$"
        )


def _require_thread_meta(request: Request, thread_id: str) -> ThreadMetadata:
    """从 ThreadManager 查询 thread metadata。"""
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    meta = next((item for item in tm.list_threads() if item.id == thread_id), None)
    if meta is None:
        raise ThreadNotFoundError(f"thread not found: {thread_id}")
    return meta


def _task_progress_manager(request: Request) -> Any:
    manager = getattr(request.app.state, "task_progress_manager", None)
    if manager is None:
        raise InvalidRequestError("task progress manager is not configured", status_code=500)
    if not callable(getattr(manager, "read_snapshot", None)) or not callable(
        getattr(manager, "write_snapshot", None)
    ):
        raise InvalidRequestError("task progress manager is invalid", status_code=500)
    return manager


def _snapshot_to_dict(snapshot: Any) -> dict[str, Any]:
    if hasattr(snapshot, "model_dump"):
        payload = snapshot.model_dump(mode="json")
    elif isinstance(snapshot, dict):
        payload = snapshot
    else:
        raise InvalidRequestError("task progress snapshot must be an object", status_code=500)
    if not isinstance(payload, dict):
        raise InvalidRequestError("task progress snapshot must be an object", status_code=500)
    return payload


def _to_payload(snapshot: Any) -> TaskProgressSnapshotPayload:
    return TaskProgressSnapshotPayload.model_validate(_snapshot_to_dict(snapshot))


def _to_web_error(exc: Exception) -> InvalidRequestError:
    if isinstance(exc, json.JSONDecodeError):
        return InvalidRequestError(f"invalid task progress json: {exc.msg}")
    if isinstance(exc, ValidationError):
        return InvalidRequestError(f"invalid task progress payload: {exc}")
    if isinstance(exc, ValueError):
        return InvalidRequestError(f"invalid task progress payload: {exc}")
    return InvalidRequestError(
        f"task progress operation failed: {type(exc).__name__}",
        status_code=500,
    )


@router.get("/{thread_id}/task-progress")
async def get_thread_task_progress(
    thread_id: str,
    request: Request,
) -> TaskProgressSnapshotPayload:
    """读取当前 thread/session 任务进度。"""
    _validate_thread_id(thread_id)
    await asyncio.to_thread(_require_thread_meta, request, thread_id)
    manager = _task_progress_manager(request)
    try:
        snapshot = await asyncio.to_thread(manager.read_snapshot, thread_id)
    except Exception as exc:
        raise _to_web_error(exc) from exc
    return _to_payload(snapshot)


@router.put("/{thread_id}/task-progress")
async def put_thread_task_progress(
    thread_id: str,
    body: PutTaskProgressRequest,
    request: Request,
) -> TaskProgressSnapshotPayload:
    """写入当前 thread/session 任务进度，source 固定为 api。"""
    _validate_thread_id(thread_id)
    await asyncio.to_thread(_require_thread_meta, request, thread_id)
    manager = _task_progress_manager(request)
    tasks = [item.model_dump() for item in body.tasks]
    try:
        snapshot = await asyncio.to_thread(manager.write_snapshot, thread_id, tasks, "api")
    except Exception as exc:
        raise _to_web_error(exc) from exc
    return _to_payload(snapshot)


__all__ = [
    "PutTaskProgressRequest",
    "TaskProgressCountsPayload",
    "TaskProgressItemPayload",
    "TaskProgressSnapshotPayload",
    "router",
]
