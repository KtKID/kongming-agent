"""Workspace 级白板持久化。

目录结构：

- ``<workspace>/board.json``：白板元数据与卡片布局
- ``<workspace>/cards/*.md``：每张卡片的 markdown 正文

设计要点：

- ``board.json`` 继续使用 Pydantic v2 schema + ``extra='forbid'`` 约束
- 写盘沿用 ``tmp + os.replace`` 原子替换风格
- 卡片正文 ``updated_at`` 直接取 markdown 文件 mtime，便于检测用户手改文件带来的冲突
- ``load_whiteboard`` 一次返回布局 + 所有卡片正文，供 ``GET /api/whiteboard`` 直接透传
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import unicodedata
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

WHITEBOARD_SCHEMA_VERSION = 1
DEFAULT_WHITEBOARD_TITLE = "Whiteboard"
DEFAULT_CARD_TITLE = "Untitled"
DEFAULT_CARD_HEIGHT = 280
DEFAULT_CARD_X = 24
DEFAULT_CARD_Y = 24

_CARD_ID_RE = r"^card-[a-f0-9]{12}$"
_FILENAME_RE = r"^[a-z0-9][a-z0-9-]{0,120}-[a-f0-9]{12}\.md$"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_LOCKS_GUARD = threading.Lock()
_WORKSPACE_LOCKS: dict[str, threading.Lock] = {}


class WhiteboardCardMeta(BaseModel):
    """单张卡片在 ``board.json`` 里的元数据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(pattern=_CARD_ID_RE)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    category: Annotated[str, Field(max_length=100)] = ""
    x: Annotated[int, Field(ge=0)] = DEFAULT_CARD_X
    y: Annotated[int, Field(ge=0)] = DEFAULT_CARD_Y
    height: Annotated[int, Field(ge=120, le=4000)] = DEFAULT_CARD_HEIGHT
    collapsed: bool = False
    z_index: Annotated[int, Field(ge=0)] = 0
    filename: Annotated[str, Field(pattern=_FILENAME_RE)]


class WhiteboardMeta(BaseModel):
    """白板根元数据，对应 ``board.json``。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=200)] = DEFAULT_WHITEBOARD_TITLE
    cards: list[WhiteboardCardMeta] = Field(default_factory=list)
    schema_version: Literal[1] = 1


class WhiteboardCardRecord(BaseModel):
    """单张卡片完整快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(pattern=_CARD_ID_RE)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    category: Annotated[str, Field(max_length=100)] = ""
    x: Annotated[int, Field(ge=0)]
    y: Annotated[int, Field(ge=0)]
    height: Annotated[int, Field(ge=120, le=4000)]
    collapsed: bool
    z_index: Annotated[int, Field(ge=0)]
    filename: Annotated[str, Field(pattern=_FILENAME_RE)]
    content: str
    updated_at: float


class WhiteboardSnapshot(BaseModel):
    """白板聚合快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=200)]
    cards: list[WhiteboardCardRecord] = Field(default_factory=list)
    schema_version: Literal[1] = 1


class WhiteboardLayoutUpdate(BaseModel):
    """单张卡片布局更新。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(pattern=_CARD_ID_RE)]
    x: Annotated[int, Field(ge=0)]
    y: Annotated[int, Field(ge=0)]
    height: Annotated[int, Field(ge=120, le=4000)]
    collapsed: bool
    z_index: Annotated[int, Field(ge=0)]


class WhiteboardContentConflictError(Exception):
    """卡片正文保存时的轻量并发冲突。"""

    def __init__(self, card_id: str, actual_updated_at: float) -> None:
        super().__init__(card_id)
        self.card_id = card_id
        self.actual_updated_at = actual_updated_at


class WhiteboardInvalidLayoutError(Exception):
    """白板布局请求本身非法。"""


def whiteboard_board_path(workspace: Path) -> Path:
    return workspace / "board.json"


def whiteboard_cards_dir(workspace: Path) -> Path:
    return workspace / "cards"


def whiteboard_card_path(workspace: Path, filename: str) -> Path:
    return whiteboard_cards_dir(workspace) / filename


def _normalize_timestamp(value: float) -> float:
    return round(float(value), 6)


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_RE.sub("-", ascii_value).strip("-")
    if not slug:
        return "card"
    return slug[:80].rstrip("-") or "card"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _workspace_lock(workspace: Path) -> threading.Lock:
    key = str(workspace.resolve(strict=False))
    with _LOCKS_GUARD:
        lock = _WORKSPACE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WORKSPACE_LOCKS[key] = lock
        return lock


def _load_board_meta(workspace: Path) -> WhiteboardMeta:
    path = whiteboard_board_path(workspace)
    if not path.is_file():
        return WhiteboardMeta()
    raw = path.read_text(encoding="utf-8")
    try:
        return WhiteboardMeta.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid whiteboard metadata at {path}: {exc}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupted whiteboard metadata at {path}: {exc}") from exc


def _write_board_meta(workspace: Path, meta: WhiteboardMeta) -> None:
    _write_atomic(
        whiteboard_board_path(workspace),
        meta.model_dump_json(indent=2),
    )


def _read_card_content(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _read_card_updated_at(path: Path) -> float:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return 0.0
    return _normalize_timestamp(stat.st_mtime_ns / 1_000_000_000)


def _to_card_record(workspace: Path, meta: WhiteboardCardMeta) -> WhiteboardCardRecord:
    path = whiteboard_card_path(workspace, meta.filename)
    return WhiteboardCardRecord(
        id=meta.id,
        title=meta.title,
        category=meta.category,
        x=meta.x,
        y=meta.y,
        height=meta.height,
        collapsed=meta.collapsed,
        z_index=meta.z_index,
        filename=meta.filename,
        content=_read_card_content(path),
        updated_at=_read_card_updated_at(path),
    )


def _find_card_index(cards: list[WhiteboardCardMeta], card_id: str) -> int:
    for index, card in enumerate(cards):
        if card.id == card_id:
            return index
    raise KeyError(card_id)


def _next_z_index(cards: list[WhiteboardCardMeta]) -> int:
    if not cards:
        return 0
    return max(card.z_index for card in cards) + 1


def _new_card_id(cards: list[WhiteboardCardMeta]) -> str:
    existing = {card.id for card in cards}
    while True:
        card_id = f"card-{secrets.token_hex(6)}"
        if card_id not in existing:
            return card_id


def load_whiteboard(workspace: Path) -> WhiteboardSnapshot:
    """加载整个白板快照。"""
    meta = _load_board_meta(workspace)
    return WhiteboardSnapshot(
        title=meta.title,
        cards=[_to_card_record(workspace, card) for card in meta.cards],
        schema_version=meta.schema_version,
    )


def create_card(
    workspace: Path,
    *,
    title: str = DEFAULT_CARD_TITLE,
    category: str = "",
    content: str = "",
    x: int = DEFAULT_CARD_X,
    y: int = DEFAULT_CARD_Y,
    height: int = DEFAULT_CARD_HEIGHT,
    collapsed: bool = False,
) -> WhiteboardCardRecord:
    """创建卡片并落盘。"""
    with _workspace_lock(workspace):
        board = _load_board_meta(workspace)
        card_id = _new_card_id(board.cards)
        filename = f"{_slugify(title)}-{card_id.removeprefix('card-')}.md"
        meta = WhiteboardCardMeta(
            id=card_id,
            title=title,
            category=category,
            x=x,
            y=y,
            height=height,
            collapsed=collapsed,
            z_index=_next_z_index(board.cards),
            filename=filename,
        )
        card_path = whiteboard_card_path(workspace, filename)
        updated_board = board.model_copy(update={"cards": [*board.cards, meta]})

        _write_atomic(card_path, content)
        try:
            _write_board_meta(workspace, updated_board)
        except Exception:
            _best_effort_unlink(card_path)
            raise
        return _to_card_record(workspace, meta)


def update_card_content(
    workspace: Path,
    card_id: str,
    *,
    content: str,
    expected_updated_at: float | None,
    title: str | None = None,
    category: str | None = None,
) -> WhiteboardCardRecord:
    """更新卡片正文，支持基于 ``updated_at`` 的轻量冲突检测。"""
    with _workspace_lock(workspace):
        board = _load_board_meta(workspace)
        card_index = _find_card_index(board.cards, card_id)
        current_meta = board.cards[card_index]
        card_path = whiteboard_card_path(workspace, current_meta.filename)
        actual_updated_at = _read_card_updated_at(card_path)
        if (
            expected_updated_at is not None
            and _normalize_timestamp(expected_updated_at) != actual_updated_at
        ):
            raise WhiteboardContentConflictError(card_id, actual_updated_at)

        previous_content = _read_card_content(card_path)
        _write_atomic(card_path, content)

        if title is None and category is None:
            return _to_card_record(workspace, current_meta)

        updated_meta = current_meta.model_copy(
            update={
                "title": current_meta.title if title is None else title,
                "category": current_meta.category if category is None else category,
            }
        )
        updated_cards = list(board.cards)
        updated_cards[card_index] = updated_meta
        try:
            _write_board_meta(workspace, board.model_copy(update={"cards": updated_cards}))
        except Exception:
            _write_atomic(card_path, previous_content)
            raise
        return _to_card_record(workspace, updated_meta)


def update_whiteboard_layout(
    workspace: Path,
    *,
    title: str | None = None,
    card_layouts: list[WhiteboardLayoutUpdate],
) -> WhiteboardSnapshot:
    """更新白板标题与卡片布局。"""
    with _workspace_lock(workspace):
        board = _load_board_meta(workspace)
        layout_by_id: dict[str, WhiteboardLayoutUpdate] = {}
        for layout in card_layouts:
            if layout.id in layout_by_id:
                raise WhiteboardInvalidLayoutError(f"duplicate card layout entry: {layout.id}")
            layout_by_id[layout.id] = layout

        if layout_by_id:
            existing_ids = {card.id for card in board.cards}
            missing_ids = [card_id for card_id in layout_by_id if card_id not in existing_ids]
            if missing_ids:
                raise KeyError(missing_ids[0])

        updated_cards: list[WhiteboardCardMeta] = []
        for card in board.cards:
            card_layout: WhiteboardLayoutUpdate | None = layout_by_id.get(card.id)
            if card_layout is None:
                updated_cards.append(card)
                continue
            updated_cards.append(
                card.model_copy(
                    update={
                        "x": card_layout.x,
                        "y": card_layout.y,
                        "height": card_layout.height,
                        "collapsed": card_layout.collapsed,
                        "z_index": card_layout.z_index,
                    }
                )
            )

        updated_board = board.model_copy(
            update={
                "title": board.title if title is None else title,
                "cards": updated_cards,
            }
        )
        _write_board_meta(workspace, updated_board)
        return load_whiteboard(workspace)


def delete_card(workspace: Path, card_id: str) -> None:
    """删除卡片。"""
    with _workspace_lock(workspace):
        board = _load_board_meta(workspace)
        card_index = _find_card_index(board.cards, card_id)
        target = board.cards[card_index]
        updated_cards = [card for card in board.cards if card.id != card_id]
        next_board = board.model_copy(update={"cards": updated_cards})
        card_path = whiteboard_card_path(workspace, target.filename)
        previous_content = _read_card_content(card_path)
        _write_board_meta(workspace, next_board)
        try:
            card_path.unlink(missing_ok=True)
        except Exception:
            _write_board_meta(workspace, board)
            _write_atomic(card_path, previous_content)
            raise


__all__ = [
    "DEFAULT_CARD_HEIGHT",
    "DEFAULT_CARD_TITLE",
    "DEFAULT_WHITEBOARD_TITLE",
    "WHITEBOARD_SCHEMA_VERSION",
    "WhiteboardCardMeta",
    "WhiteboardCardRecord",
    "WhiteboardContentConflictError",
    "WhiteboardInvalidLayoutError",
    "WhiteboardLayoutUpdate",
    "WhiteboardMeta",
    "WhiteboardSnapshot",
    "create_card",
    "delete_card",
    "load_whiteboard",
    "update_card_content",
    "update_whiteboard_layout",
    "whiteboard_board_path",
    "whiteboard_card_path",
    "whiteboard_cards_dir",
]
