"""whiteboard_store 单测。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hosts.web.whiteboard.store import (
    DEFAULT_WHITEBOARD_TITLE,
    WhiteboardContentConflictError,
    WhiteboardLayoutUpdate,
    create_card,
    delete_card,
    load_whiteboard,
    update_card_content,
    update_whiteboard_layout,
    whiteboard_board_path,
    whiteboard_card_path,
)


def test_load_whiteboard_defaults_when_missing(tmp_path: Path) -> None:
    snapshot = load_whiteboard(tmp_path)
    assert snapshot.title == DEFAULT_WHITEBOARD_TITLE
    assert snapshot.cards == []
    assert snapshot.schema_version == 1


def test_create_card_writes_board_and_markdown(tmp_path: Path) -> None:
    card = create_card(
        tmp_path,
        title="Sprint Notes",
        category="planning",
        content="# hello",
        x=80,
        y=120,
        height=360,
        collapsed=False,
    )

    board_path = whiteboard_board_path(tmp_path)
    card_path = whiteboard_card_path(tmp_path, card.filename)

    assert board_path.is_file()
    assert card_path.is_file()
    assert card_path.read_text(encoding="utf-8") == "# hello"
    assert not board_path.with_name("board.json.tmp").exists()

    snapshot = load_whiteboard(tmp_path)
    assert snapshot.cards[0].id == card.id
    assert snapshot.cards[0].title == "Sprint Notes"
    assert snapshot.cards[0].category == "planning"
    assert snapshot.cards[0].content == "# hello"


def test_update_card_content_detects_stale_expected_updated_at(tmp_path: Path) -> None:
    card = create_card(tmp_path, title="Note", content="v1")
    path = whiteboard_card_path(tmp_path, card.filename)

    time.sleep(0.02)
    path.write_text("external change", encoding="utf-8")

    with pytest.raises(WhiteboardContentConflictError) as exc_info:
        update_card_content(
            tmp_path,
            card.id,
            content="v2",
            expected_updated_at=card.updated_at,
        )

    assert exc_info.value.card_id == card.id
    assert exc_info.value.actual_updated_at > card.updated_at


def test_update_card_content_updates_text_and_meta(tmp_path: Path) -> None:
    card = create_card(tmp_path, title="Old", category="draft", content="v1")

    updated = update_card_content(
        tmp_path,
        card.id,
        content="v2",
        expected_updated_at=card.updated_at,
        title="New",
        category="final",
    )

    assert updated.title == "New"
    assert updated.category == "final"
    assert updated.content == "v2"
    assert updated.updated_at >= card.updated_at

    snapshot = load_whiteboard(tmp_path)
    assert snapshot.cards[0].title == "New"
    assert snapshot.cards[0].category == "final"


def test_update_whiteboard_layout_updates_title_and_cards(tmp_path: Path) -> None:
    first = create_card(tmp_path, title="A", x=10, y=20, height=280)
    second = create_card(tmp_path, title="B", x=30, y=40, height=300)

    snapshot = update_whiteboard_layout(
        tmp_path,
        title="Project Board",
        card_layouts=[
            WhiteboardLayoutUpdate(
                id=first.id,
                x=200,
                y=210,
                height=420,
                collapsed=True,
                z_index=5,
            ),
            WhiteboardLayoutUpdate(
                id=second.id,
                x=220,
                y=240,
                height=360,
                collapsed=False,
                z_index=6,
            ),
        ],
    )

    assert snapshot.title == "Project Board"
    first_after = next(card for card in snapshot.cards if card.id == first.id)
    assert first_after.x == 200
    assert first_after.y == 210
    assert first_after.height == 420
    assert first_after.collapsed is True
    assert first_after.z_index == 5


def test_delete_card_removes_board_entry_and_markdown(tmp_path: Path) -> None:
    card = create_card(tmp_path, title="Delete Me", content="bye")
    path = whiteboard_card_path(tmp_path, card.filename)
    assert path.is_file()

    delete_card(tmp_path, card.id)

    assert not path.exists()
    snapshot = load_whiteboard(tmp_path)
    assert snapshot.cards == []
