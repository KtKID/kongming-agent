"""Thread-scoped 白板 REST 路由（双 scope：project + global）。

路由全部挂在 ``/api/threads/{thread_id}/whiteboard`` 下，cwd 通过 thread metadata
解析：``meta.cwd`` 为空串时只读 global 白板，``scope="project"`` 的写操作会被
:class:`WhiteboardScopeError` 拒绝（router 转 422）。

数据访问统一走 :class:`web.whiteboard_manager.WhiteboardManager`——
``.importlinter`` Contract 10 强制 router 不可直接 import
``web.whiteboard_store``，所有 store 类型（``WhiteboardLayoutUpdate`` /
``WhiteboardInvalidLayoutError`` / ``WhiteboardContentConflictError``）由 manager
re-export。
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Request
from pydantic import ValidationError

from web.errors import InvalidThreadIdError, KongmingWebError, ThreadNotFoundError
from web.protocol import (
    CreateWhiteboardCardRequest,
    UpdateWhiteboardCardRequest,
    UpdateWhiteboardLayoutRequest,
    WhiteboardCardDTO,
    WhiteboardDTO,
)
from web.routers.threads import THREAD_ID_RE
from web.whiteboard_manager import (
    ScopedCardRecord,
    ScopedWhiteboardSnapshot,
    WhiteboardContentConflictError,
    WhiteboardInvalidLayoutError,
    WhiteboardLayoutUpdate,
    WhiteboardManager,
    WhiteboardScopeError,
)

if TYPE_CHECKING:
    from web.threads.metadata import ThreadMetadata
    from web.threads.types import ThreadManagerProtocol

router = APIRouter(prefix="/api/threads", tags=["whiteboard"])

CARD_ID_RE: re.Pattern[str] = re.compile(r"^card-[a-f0-9]{12}$")


class InvalidWhiteboardCardIdError(KongmingWebError):
    status_code = 422


class WhiteboardCardNotFoundError(KongmingWebError):
    status_code = 404


class WhiteboardConflictError(KongmingWebError):
    status_code = 409


class WhiteboardStorageError(KongmingWebError):
    status_code = 500


class WhiteboardLayoutValidationError(KongmingWebError):
    status_code = 422


class WhiteboardScopeRejectedError(KongmingWebError):
    status_code = 422


def _validate_card_id(card_id: str) -> None:
    if not CARD_ID_RE.match(card_id):
        raise InvalidWhiteboardCardIdError(
            f"invalid card_id: {card_id!r}; must match ^card-[a-f0-9]{{12}}$"
        )


def _validate_thread_id(thread_id: str) -> None:
    if not THREAD_ID_RE.match(thread_id):
        raise InvalidThreadIdError(
            f"invalid thread_id: {thread_id!r}; must match ^thread-[a-f0-9]{{12}}$"
        )


def _require_thread_meta(request: Request, thread_id: str) -> ThreadMetadata:
    """从 ``app.state.thread_manager`` 找 thread；不存在抛 404。"""
    tm: ThreadManagerProtocol = request.app.state.thread_manager
    meta: ThreadMetadata | None = next(
        (item for item in tm.list_threads() if item.id == thread_id),
        None,
    )
    if meta is None:
        raise ThreadNotFoundError(f"thread not found: {thread_id}")
    return meta


def _resolve_cwd(meta: ThreadMetadata) -> str | None:
    """把 thread metadata 的 ``cwd`` 字段规整成 manager API 的 ``str | None``。

    空串与 ``None`` 等价（manager 内部都走 "无项目 cwd" 分支）。
    """
    cwd = meta.cwd.strip()
    return cwd or None


def _whiteboard_manager(request: Request) -> WhiteboardManager:
    return cast(WhiteboardManager, request.app.state.whiteboard_manager)


def _to_card_dto(scoped: ScopedCardRecord) -> WhiteboardCardDTO:
    card = scoped.record
    return WhiteboardCardDTO(
        id=card.id,
        scope=scoped.scope,
        title=card.title,
        category=card.category,
        x=card.x,
        y=card.y,
        height=card.height,
        collapsed=card.collapsed,
        z_index=card.z_index,
        filename=card.filename,
        content=card.content,
        updated_at=card.updated_at,
    )


def _to_board_dto(snapshot: ScopedWhiteboardSnapshot) -> WhiteboardDTO:
    return WhiteboardDTO(
        global_title=snapshot.global_title,
        project_title=snapshot.project_title,
        cards=[_to_card_dto(card) for card in snapshot.cards],
    )


def _handle_manager_error(exc: Exception) -> None:
    """把 manager / store 抛出的异常翻译成 web 层错误。

    分类：

    - :class:`WhiteboardScopeError` → 422（``scope="project"`` 但 cwd 为空）
    - :class:`KeyError` → 404（卡片在两个 scope 都找不到）
    - :class:`WhiteboardContentConflictError` → 409（乐观锁冲突）
    - :class:`WhiteboardInvalidLayoutError` → 422（layout payload 合法性）
    - :class:`OSError` / :class:`ValidationError` / :class:`ValueError` → 500
    - 其余原样抛出
    """
    # WhiteboardScopeError 继承 ValueError，必须先于 ValueError 分支命中
    if isinstance(exc, WhiteboardScopeError):
        raise WhiteboardScopeRejectedError(str(exc)) from exc
    if isinstance(exc, KeyError):
        raise WhiteboardCardNotFoundError(f"whiteboard card not found: {exc.args[0]}") from exc
    if isinstance(exc, WhiteboardContentConflictError):
        raise WhiteboardConflictError(
            f"whiteboard card content conflict: {exc.card_id}; "
            f"expected stale version, latest updated_at={exc.actual_updated_at}"
        ) from exc
    if isinstance(exc, WhiteboardInvalidLayoutError):
        raise WhiteboardLayoutValidationError(str(exc)) from exc
    if isinstance(exc, (OSError, ValidationError, ValueError)):
        raise WhiteboardStorageError(str(exc)) from exc
    raise exc


@router.get("/{thread_id}/whiteboard")
async def get_whiteboard(thread_id: str, request: Request) -> WhiteboardDTO:
    _validate_thread_id(thread_id)
    manager = _whiteboard_manager(request)
    meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
    cwd = _resolve_cwd(meta)
    try:
        snapshot = await asyncio.to_thread(manager.load, cwd)
    except Exception as exc:
        _handle_manager_error(exc)
    return _to_board_dto(snapshot)


@router.post("/{thread_id}/whiteboard/cards", status_code=201)
async def post_whiteboard_card(
    thread_id: str,
    body: CreateWhiteboardCardRequest,
    request: Request,
) -> WhiteboardCardDTO:
    _validate_thread_id(thread_id)
    manager = _whiteboard_manager(request)
    meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
    cwd = _resolve_cwd(meta)
    try:
        scoped = await asyncio.to_thread(
            manager.create_card,
            cwd,
            scope=body.scope,
            title=body.title,
            category=body.category,
            content=body.content,
            x=body.x,
            y=body.y,
            height=body.height,
            collapsed=body.collapsed,
        )
    except Exception as exc:
        _handle_manager_error(exc)
    return _to_card_dto(scoped)


@router.put("/{thread_id}/whiteboard/cards/{card_id}")
async def put_whiteboard_card(
    thread_id: str,
    card_id: str,
    body: UpdateWhiteboardCardRequest,
    request: Request,
) -> WhiteboardCardDTO:
    _validate_thread_id(thread_id)
    _validate_card_id(card_id)
    manager = _whiteboard_manager(request)
    meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
    cwd = _resolve_cwd(meta)
    try:
        scoped = await asyncio.to_thread(
            manager.update_card_content,
            cwd,
            card_id,
            content=body.content,
            expected_updated_at=body.expected_updated_at,
            title=body.title,
            category=body.category,
        )
    except Exception as exc:
        _handle_manager_error(exc)
    return _to_card_dto(scoped)


@router.patch("/{thread_id}/whiteboard/layout")
async def patch_whiteboard_layout(
    thread_id: str,
    body: UpdateWhiteboardLayoutRequest,
    request: Request,
) -> WhiteboardDTO:
    _validate_thread_id(thread_id)
    manager = _whiteboard_manager(request)
    meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
    cwd = _resolve_cwd(meta)
    try:
        snapshot = await asyncio.to_thread(
            manager.update_layout,
            cwd,
            scope=body.scope,
            title=body.title,
            card_layouts=[
                WhiteboardLayoutUpdate(
                    id=card.id,
                    x=card.x,
                    y=card.y,
                    height=card.height,
                    collapsed=card.collapsed,
                    z_index=card.z_index,
                )
                for card in body.cards
            ],
        )
    except Exception as exc:
        _handle_manager_error(exc)
    return _to_board_dto(snapshot)


@router.delete("/{thread_id}/whiteboard/cards/{card_id}", status_code=204)
async def delete_whiteboard_card(
    thread_id: str,
    card_id: str,
    request: Request,
) -> None:
    _validate_thread_id(thread_id)
    _validate_card_id(card_id)
    manager = _whiteboard_manager(request)
    meta = await asyncio.to_thread(_require_thread_meta, request, thread_id)
    cwd = _resolve_cwd(meta)
    try:
        await asyncio.to_thread(manager.delete_card, cwd, card_id)
    except Exception as exc:
        _handle_manager_error(exc)


__all__ = ["CARD_ID_RE", "router"]
