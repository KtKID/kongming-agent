"""Thread CRUD 路由。

端点：

- ``GET    /api/threads``               — 列出所有 thread metadata
- ``POST   /api/threads``               — 创建 thread；返回 metadata（201）
- ``PATCH  /api/threads/{thread_id}``    — 重命名（不改 preset）
- ``DELETE /api/threads/{thread_id}``    — 删除（204）

注意：**没有** ``GET /api/threads/{id}/history`` —— 历史走 WS ``thread.history`` 帧。

安全：

- ``thread_id`` 必须匹配 ``^thread-[a-f0-9]{12}$``；不匹配抛 422
  :class:`InvalidThreadIdError`，防 path traversal。
- ``ThreadManager.delete_thread`` 幂等（thread 不存在不抛），但这里**不**对外
  暴露幂等性 —— 不存在抛 :class:`ThreadNotFoundError(404)`，符合 REST 语义。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from web.errors import InvalidThreadIdError, ThreadNotFoundError
from web.protocol import (
    CreateThreadRequest,
    RenameThreadRequest,
    ThreadMetadataDTO,
)

if TYPE_CHECKING:
    from web.thread_metadata import ThreadMetadata
    from web.types import ThreadManagerProtocol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/threads", tags=["threads"])

THREAD_ID_RE: re.Pattern[str] = re.compile(r"^thread-[a-f0-9]{12}$")


def _validate_thread_id(thread_id: str) -> None:
    if not THREAD_ID_RE.match(thread_id):
        raise InvalidThreadIdError(
            f"invalid thread_id: {thread_id!r}; must match ^thread-[a-f0-9]{{12}}$"
        )


def _to_dto(meta: ThreadMetadata) -> ThreadMetadataDTO:
    return ThreadMetadataDTO(
        id=meta.id,
        name=meta.name,
        preset_id=meta.preset_id,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        message_count=meta.message_count,
        schema_version=meta.schema_version,
    )


@router.get("")
async def list_threads(request: Request) -> list[ThreadMetadataDTO]:
    """列出所有 thread metadata。

    实现：``ThreadManager.list_threads()`` 是同步 IO（扫盘），用
    :func:`asyncio.to_thread` 隔离事件循环。
    """
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    metas = await asyncio.to_thread(tm.list_threads)
    return [_to_dto(m) for m in metas]


@router.post("", status_code=201)
async def create_thread(
    body: CreateThreadRequest,
    request: Request,
) -> ThreadMetadataDTO:
    """创建 thread。"""
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    meta = await tm.create_thread(body.name, body.preset_id)
    return _to_dto(meta)


@router.patch("/{thread_id}")
async def rename_thread(
    thread_id: str,
    body: RenameThreadRequest,
    request: Request,
) -> ThreadMetadataDTO:
    """重命名 thread。"""
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    try:
        meta = await tm.rename_thread(thread_id, body.name)
    except KeyError as exc:
        raise ThreadNotFoundError(f"thread not found: {thread_id}") from exc
    return _to_dto(meta)


@router.delete("/{thread_id}", status_code=204)
async def delete_thread(thread_id: str, request: Request) -> None:
    """删除 thread。

    语义：thread 不存在 → 404。``ThreadManager.delete_thread`` 自身幂等，
    但 REST 层显式查存在性以返回 404 —— 用 ``list_threads`` 反查（开销可
    接受，v0.1.5 thread 数量少）。
    """
    _validate_thread_id(thread_id)
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    metas = await asyncio.to_thread(tm.list_threads)
    if not any(m.id == thread_id for m in metas):
        raise ThreadNotFoundError(f"thread not found: {thread_id}")
    await tm.delete_thread(thread_id)


__all__ = ["THREAD_ID_RE", "router"]
