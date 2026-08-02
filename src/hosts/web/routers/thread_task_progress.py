"""Thread 任务进度只读 REST router。

本模块把 SessionTaskProgressManager 持有的 foreground workflow 快照暴露为只读 HTTP 资源。
关键流程：验证 thread 身份，读取注入 Manager 的快照，使用 protocol REST DTO 校验后返回。
关键函数：get_thread_task_progress 读取当前快照，_to_payload 收口后端到 wire 的模型校验。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from pydantic import ValidationError

from hosts.web.errors import InvalidRequestError, InvalidThreadIdError, ThreadNotFoundError
from hosts.web.protocol import TaskProgressSnapshotPayload
from hosts.web.routers.threads import THREAD_ID_RE

if TYPE_CHECKING:
    from hosts.web.threads.metadata import ThreadMetadata
    from hosts.web.threads.types import ThreadManagerProtocol

router = APIRouter(prefix="/api/threads", tags=["thread-task-progress"])


def _validate_thread_id(thread_id: str) -> None:
    """校验 thread ID 格式，输入为路径参数，非法时抛 Web 错误。"""
    if not THREAD_ID_RE.match(thread_id):
        raise InvalidThreadIdError(
            f"invalid thread_id: {thread_id!r}; must match ^thread-[a-f0-9]{{12}}$"
        )


def _require_thread_meta(request: Request, thread_id: str) -> ThreadMetadata:
    """读取 thread 元数据，输入为请求和 thread ID，缺失时抛 404。"""
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    meta = next((item for item in tm.list_threads() if item.id == thread_id), None)
    if meta is None:
        raise ThreadNotFoundError(f"thread not found: {thread_id}")
    return meta


def _task_progress_manager(request: Request) -> Any:
    """解析只读进度 Manager，输入为请求，输出为可调用的 duck-typed 注入对象。"""
    manager = getattr(request.app.state, "task_progress_manager", None)
    if manager is None or not callable(getattr(manager, "read_snapshot", None)):
        raise InvalidRequestError("task progress manager is not configured", status_code=500)
    return manager


def _to_payload(snapshot: object) -> TaskProgressSnapshotPayload:
    """校验 Manager 快照为协议 DTO，输入为快照对象，输出为安全 REST payload。"""
    if not hasattr(snapshot, "model_dump"):
        raise InvalidRequestError(
            "task progress snapshot must be a protocol model", status_code=500
        )
    raw = snapshot.model_dump(mode="json")
    if not isinstance(raw, dict):
        raise InvalidRequestError("task progress snapshot must be an object", status_code=500)
    return TaskProgressSnapshotPayload.model_validate(raw)


def _to_web_error(exc: Exception) -> InvalidRequestError:
    """归一化快照读取错误，输入为异常，输出为客户端可理解的 Web 错误。"""
    if isinstance(exc, json.JSONDecodeError):
        return InvalidRequestError(f"invalid task progress json: {exc.msg}")
    if isinstance(exc, (ValidationError, ValueError)):
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
    """读取当前 thread 的 foreground workflow 快照。"""
    _validate_thread_id(thread_id)
    await asyncio.to_thread(_require_thread_meta, request, thread_id)
    manager = _task_progress_manager(request)
    try:
        snapshot = await asyncio.to_thread(manager.read_snapshot, thread_id)
        return _to_payload(snapshot)
    except Exception as exc:
        raise _to_web_error(exc) from exc


__all__ = ["router"]
