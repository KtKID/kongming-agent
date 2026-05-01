"""Workspace 级白板 REST 路由。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import cast

from fastapi import APIRouter, Request
from pydantic import ValidationError

from web.errors import KongmingWebError
from web.protocol import (
    CreateWhiteboardCardRequest,
    UpdateWhiteboardCardRequest,
    UpdateWhiteboardLayoutRequest,
    WhiteboardCardDTO,
    WhiteboardDTO,
)
from web.whiteboard_store import (
    WhiteboardCardRecord,
    WhiteboardContentConflictError,
    WhiteboardInvalidLayoutError,
    WhiteboardLayoutUpdate,
    WhiteboardSnapshot,
    create_card,
    delete_card,
    load_whiteboard,
    update_card_content,
    update_whiteboard_layout,
)

router = APIRouter(prefix="/api/whiteboard", tags=["whiteboard"])

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


def _validate_card_id(card_id: str) -> None:
    if not CARD_ID_RE.match(card_id):
        raise InvalidWhiteboardCardIdError(
            f"invalid card_id: {card_id!r}; must match ^card-[a-f0-9]{{12}}$"
        )


def _workspace_root(request: Request) -> Path:
    return cast(Path, request.app.state.workspace_root)


def _to_card_dto(card: WhiteboardCardRecord) -> WhiteboardCardDTO:
    return WhiteboardCardDTO(
        id=card.id,
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


def _to_board_dto(snapshot: WhiteboardSnapshot) -> WhiteboardDTO:
    return WhiteboardDTO(
        title=snapshot.title,
        cards=[_to_card_dto(card) for card in snapshot.cards],
        schema_version=snapshot.schema_version,
    )


def _handle_store_error(exc: Exception) -> None:
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


@router.get("")
async def get_whiteboard(request: Request) -> WhiteboardDTO:
    workspace = _workspace_root(request)
    try:
        snapshot = await asyncio.to_thread(load_whiteboard, workspace)
    except Exception as exc:
        _handle_store_error(exc)
    return _to_board_dto(snapshot)


@router.post("/cards", status_code=201)
async def post_whiteboard_card(
    body: CreateWhiteboardCardRequest,
    request: Request,
) -> WhiteboardCardDTO:
    workspace = _workspace_root(request)
    try:
        card = await asyncio.to_thread(
            create_card,
            workspace,
            title=body.title,
            category=body.category,
            content=body.content,
            x=body.x,
            y=body.y,
            height=body.height,
            collapsed=body.collapsed,
        )
    except Exception as exc:
        _handle_store_error(exc)
    return _to_card_dto(card)


@router.put("/cards/{card_id}")
async def put_whiteboard_card(
    card_id: str,
    body: UpdateWhiteboardCardRequest,
    request: Request,
) -> WhiteboardCardDTO:
    _validate_card_id(card_id)
    workspace = _workspace_root(request)
    try:
        card = await asyncio.to_thread(
            update_card_content,
            workspace,
            card_id,
            content=body.content,
            expected_updated_at=body.expected_updated_at,
            title=body.title,
            category=body.category,
        )
    except Exception as exc:
        _handle_store_error(exc)
    return _to_card_dto(card)


@router.patch("/layout")
async def patch_whiteboard_layout(
    body: UpdateWhiteboardLayoutRequest,
    request: Request,
) -> WhiteboardDTO:
    workspace = _workspace_root(request)
    try:
        snapshot = await asyncio.to_thread(
            update_whiteboard_layout,
            workspace,
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
        _handle_store_error(exc)
    return _to_board_dto(snapshot)


@router.delete("/cards/{card_id}", status_code=204)
async def delete_whiteboard_card(card_id: str, request: Request) -> None:
    _validate_card_id(card_id)
    workspace = _workspace_root(request)
    try:
        await asyncio.to_thread(delete_card, workspace, card_id)
    except Exception as exc:
        _handle_store_error(exc)


__all__ = ["CARD_ID_RE", "router"]
