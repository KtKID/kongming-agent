"""WhiteboardManager 单测（覆盖 spec 05 的 M1~M15 + E1~E2 共 17 个场景）。

测试隔离：每个用例用独立 ``tmp_path``，避免 ``_WORKSPACE_LOCKS`` 模块级 dict 跨用例污染。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hosts.web.whiteboard.manager import (
    ScopedCardRecord,
    ScopedWhiteboardSnapshot,
    WhiteboardManager,
    WhiteboardScopeError,
    encode_project_dir,
)
from hosts.web.whiteboard.store import (
    WhiteboardContentConflictError,
    WhiteboardLayoutUpdate,
)


@pytest.fixture
def manager(tmp_path: Path) -> WhiteboardManager:
    """每个用例独享 ``whiteboard_root``，store 内部 ``_WORKSPACE_LOCKS`` 互不污染。"""
    return WhiteboardManager(whiteboard_root=tmp_path)


# ---------------------------------------------------------------------------
# M1：create_card(cwd=None, scope="global")
# ---------------------------------------------------------------------------


def test_m1_create_global_card_writes_to_global_workspace(
    manager: WhiteboardManager, tmp_path: Path
) -> None:
    record = manager.create_card(
        cwd=None,
        scope="global",
        title="Global Note",
        content="hello global",
    )

    assert isinstance(record, ScopedCardRecord)
    assert record.scope == "global"
    assert record.record.title == "Global Note"
    assert record.record.content == "hello global"

    # 内容落 <root>/global/cards/
    global_cards_dir = tmp_path / "global" / "cards"
    assert global_cards_dir.is_dir()
    md_files = list(global_cards_dir.glob("*.md"))
    assert len(md_files) == 1
    assert md_files[0].read_text(encoding="utf-8") == "hello global"

    # layout 写 global board.json
    global_board = tmp_path / "global" / "board.json"
    assert global_board.is_file()

    # project 目录未被创建
    assert not (tmp_path / "projects").exists()


# ---------------------------------------------------------------------------
# M2：create_card(cwd="/a", scope="project")
# ---------------------------------------------------------------------------


def test_m2_create_project_card_writes_meta_and_layout(
    manager: WhiteboardManager, tmp_path: Path
) -> None:
    cwd = "/Users/test/proj-a"
    record = manager.create_card(
        cwd=cwd,
        scope="project",
        title="Project Note",
        content="project body",
    )

    assert record.scope == "project"

    encoded = encode_project_dir(cwd)
    project_ws = tmp_path / "projects" / encoded

    # meta.json 写入
    meta_path = project_ws / "meta.json"
    assert meta_path.is_file()
    assert cwd in meta_path.read_text(encoding="utf-8")

    # 内容文件
    cards_dir = project_ws / "cards"
    assert cards_dir.is_dir()
    md_files = list(cards_dir.glob("*.md"))
    assert len(md_files) == 1
    assert md_files[0].read_text(encoding="utf-8") == "project body"

    # project board.json
    assert (project_ws / "board.json").is_file()


# ---------------------------------------------------------------------------
# M3：create_card(cwd=None, scope="project") → WhiteboardScopeError
# ---------------------------------------------------------------------------


def test_m3_create_project_card_without_cwd_raises(manager: WhiteboardManager) -> None:
    with pytest.raises(WhiteboardScopeError):
        manager.create_card(cwd=None, scope="project", title="x", content="")

    with pytest.raises(WhiteboardScopeError):
        # 空字符串同样视为缺失
        manager.create_card(cwd="", scope="project", title="x", content="")


# ---------------------------------------------------------------------------
# M4：load(cwd=None) 只读 global
# ---------------------------------------------------------------------------


def test_m4_load_without_cwd_returns_only_global(
    manager: WhiteboardManager,
) -> None:
    manager.create_card(cwd=None, scope="global", title="G1", content="g1")
    manager.create_card(cwd=None, scope="global", title="G2", content="g2")

    snapshot = manager.load(cwd=None)

    assert isinstance(snapshot, ScopedWhiteboardSnapshot)
    assert snapshot.project_title is None
    assert len(snapshot.cards) == 2
    assert all(card.scope == "global" for card in snapshot.cards)


# ---------------------------------------------------------------------------
# M5：load(cwd="/a") 合并 2 global + 1 project，按 z_index 排序
# ---------------------------------------------------------------------------


def test_m5_load_merges_global_and_project_sorted_by_z_index(
    manager: WhiteboardManager,
) -> None:
    cwd = "/Users/test/proj-a"
    manager.create_card(cwd=None, scope="global", title="G1", content="g1")
    manager.create_card(cwd=None, scope="global", title="G2", content="g2")
    manager.create_card(cwd=cwd, scope="project", title="P1", content="p1")

    snapshot = manager.load(cwd=cwd)

    assert len(snapshot.cards) == 3
    # 按 z_index 升序
    z_indices = [card.record.z_index for card in snapshot.cards]
    assert z_indices == sorted(z_indices)

    # scope 标注正确
    by_title = {card.record.title: card.scope for card in snapshot.cards}
    assert by_title["G1"] == "global"
    assert by_title["G2"] == "global"
    assert by_title["P1"] == "project"


# ---------------------------------------------------------------------------
# M6：load(cwd="/a")，project 目录不存在 → 只返回 global 卡 + 默认 project_title
# （cwd 非空时 project_title 不能是 None，否则前端主按钮会被锁死无法建首张卡）
# ---------------------------------------------------------------------------


def test_m6_load_when_project_dir_missing_returns_default_title(
    manager: WhiteboardManager,
) -> None:
    manager.create_card(cwd=None, scope="global", title="G1", content="g1")

    snapshot = manager.load(cwd="/Users/test/nonexistent")

    # cwd 非空 + workspace 未创建 → project_title 取默认值，**不是** None
    assert snapshot.project_title == "Whiteboard"
    assert len(snapshot.cards) == 1
    assert snapshot.cards[0].scope == "global"


# ---------------------------------------------------------------------------
# M7：update_card_content 路由到 global
# ---------------------------------------------------------------------------


def test_m7_update_global_card_via_project_cwd(
    manager: WhiteboardManager,
) -> None:
    cwd = "/Users/test/proj-a"
    g_card = manager.create_card(cwd=None, scope="global", title="G", content="v1")
    manager.create_card(cwd=cwd, scope="project", title="P", content="p1")

    updated = manager.update_card_content(
        cwd=cwd,
        card_id=g_card.record.id,
        content="v2",
        expected_updated_at=None,
        title=None,
        category=None,
    )

    assert updated.scope == "global"
    assert updated.record.id == g_card.record.id
    assert updated.record.content == "v2"


# ---------------------------------------------------------------------------
# M8：update_card_content 路由到 project
# ---------------------------------------------------------------------------


def test_m8_update_project_card(manager: WhiteboardManager) -> None:
    cwd = "/Users/test/proj-a"
    manager.create_card(cwd=None, scope="global", title="G", content="g1")
    p_card = manager.create_card(cwd=cwd, scope="project", title="P", content="p1")

    updated = manager.update_card_content(
        cwd=cwd,
        card_id=p_card.record.id,
        content="p2",
        expected_updated_at=None,
        title=None,
        category=None,
    )

    assert updated.scope == "project"
    assert updated.record.id == p_card.record.id
    assert updated.record.content == "p2"


# ---------------------------------------------------------------------------
# M9：update_card_content(cwd=None) 命中 global
# ---------------------------------------------------------------------------


def test_m9_update_global_card_with_no_cwd(manager: WhiteboardManager) -> None:
    g_card = manager.create_card(cwd=None, scope="global", title="G", content="v1")

    updated = manager.update_card_content(
        cwd=None,
        card_id=g_card.record.id,
        content="v2",
        expected_updated_at=None,
        title=None,
        category=None,
    )

    assert updated.scope == "global"
    assert updated.record.content == "v2"


# ---------------------------------------------------------------------------
# M10：delete_card(cwd="/a", card_id=X) 删 global
# ---------------------------------------------------------------------------


def test_m10_delete_global_card_removes_content_and_layout(
    manager: WhiteboardManager, tmp_path: Path
) -> None:
    cwd = "/Users/test/proj-a"
    g_card = manager.create_card(cwd=None, scope="global", title="G", content="bye")
    manager.create_card(cwd=cwd, scope="project", title="P", content="p1")

    # 删除前内容存在
    global_cards_dir = tmp_path / "global" / "cards"
    assert len(list(global_cards_dir.glob("*.md"))) == 1

    manager.delete_card(cwd=cwd, card_id=g_card.record.id)

    # 内容文件消失
    assert len(list(global_cards_dir.glob("*.md"))) == 0

    # board.json layout 中也不再有该卡片
    snapshot = manager.load(cwd=cwd)
    ids = [card.record.id for card in snapshot.cards]
    assert g_card.record.id not in ids
    # project 卡片还在
    assert len(snapshot.cards) == 1
    assert snapshot.cards[0].scope == "project"


# ---------------------------------------------------------------------------
# M11：delete_card 找不到 → KeyError
# ---------------------------------------------------------------------------


def test_m11_delete_unknown_card_raises_keyerror(
    manager: WhiteboardManager,
) -> None:
    cwd = "/Users/test/proj-a"
    manager.create_card(cwd=None, scope="global", title="G", content="g1")
    manager.create_card(cwd=cwd, scope="project", title="P", content="p1")

    with pytest.raises(KeyError):
        manager.delete_card(cwd=cwd, card_id="card-deadbeef0000")


# ---------------------------------------------------------------------------
# M12：update_card_content 冲突 → WhiteboardContentConflictError 原样传播
# ---------------------------------------------------------------------------


def test_m12_update_content_conflict_propagates(manager: WhiteboardManager) -> None:
    g_card = manager.create_card(cwd=None, scope="global", title="G", content="v1")

    # 传一个肯定不匹配的 expected_updated_at（store 用 _normalize_timestamp 比较）
    stale_ts = g_card.record.updated_at - 100.0

    with pytest.raises(WhiteboardContentConflictError) as exc_info:
        manager.update_card_content(
            cwd=None,
            card_id=g_card.record.id,
            content="v2",
            expected_updated_at=stale_ts,
            title=None,
            category=None,
        )

    assert exc_info.value.card_id == g_card.record.id


# ---------------------------------------------------------------------------
# M13：update_layout(scope="global") 只改 global board.json
# ---------------------------------------------------------------------------


def test_m13_update_layout_global_does_not_touch_project(
    manager: WhiteboardManager, tmp_path: Path
) -> None:
    cwd = "/Users/test/proj-a"
    g_card = manager.create_card(cwd=None, scope="global", title="G", content="g1", x=10, y=10)
    p_card = manager.create_card(cwd=cwd, scope="project", title="P", content="p1", x=20, y=20)

    project_board = tmp_path / "projects" / encode_project_dir(cwd) / "board.json"
    project_board_before = project_board.read_text(encoding="utf-8")

    manager.update_layout(
        cwd=cwd,
        scope="global",
        title="Global Renamed",
        card_layouts=[
            WhiteboardLayoutUpdate(
                id=g_card.record.id,
                x=500,
                y=600,
                height=300,
                collapsed=False,
                z_index=99,
            )
        ],
    )

    # project board.json 字节级未变
    assert project_board.read_text(encoding="utf-8") == project_board_before

    # global 改了：title + g_card 坐标
    snapshot = manager.load(cwd=cwd)
    assert snapshot.global_title == "Global Renamed"
    updated_g = next(c for c in snapshot.cards if c.record.id == g_card.record.id)
    assert updated_g.record.x == 500
    assert updated_g.record.y == 600
    assert updated_g.record.z_index == 99
    # project 卡片坐标不变
    unchanged_p = next(c for c in snapshot.cards if c.record.id == p_card.record.id)
    assert unchanged_p.record.x == 20
    assert unchanged_p.record.y == 20


# ---------------------------------------------------------------------------
# M14：update_layout(scope="project") 只改 project board.json
# ---------------------------------------------------------------------------


def test_m14_update_layout_project_does_not_touch_global(
    manager: WhiteboardManager, tmp_path: Path
) -> None:
    cwd = "/Users/test/proj-a"
    g_card = manager.create_card(cwd=None, scope="global", title="G", content="g1", x=10, y=10)
    p_card = manager.create_card(cwd=cwd, scope="project", title="P", content="p1", x=20, y=20)

    global_board = tmp_path / "global" / "board.json"
    global_board_before = global_board.read_text(encoding="utf-8")

    manager.update_layout(
        cwd=cwd,
        scope="project",
        title="Project Renamed",
        card_layouts=[
            WhiteboardLayoutUpdate(
                id=p_card.record.id,
                x=700,
                y=800,
                height=400,
                collapsed=True,
                z_index=55,
            )
        ],
    )

    # global board.json 字节级未变
    assert global_board.read_text(encoding="utf-8") == global_board_before

    snapshot = manager.load(cwd=cwd)
    assert snapshot.project_title == "Project Renamed"
    updated_p = next(c for c in snapshot.cards if c.record.id == p_card.record.id)
    assert updated_p.record.x == 700
    assert updated_p.record.y == 800
    assert updated_p.record.collapsed is True
    assert updated_p.record.z_index == 55
    # global 卡片坐标不变
    unchanged_g = next(c for c in snapshot.cards if c.record.id == g_card.record.id)
    assert unchanged_g.record.x == 10
    assert unchanged_g.record.y == 10


# ---------------------------------------------------------------------------
# M15：update_layout(cwd=None, scope="project") → WhiteboardScopeError
# ---------------------------------------------------------------------------


def test_m15_update_layout_project_without_cwd_raises(
    manager: WhiteboardManager,
) -> None:
    with pytest.raises(WhiteboardScopeError):
        manager.update_layout(
            cwd=None,
            scope="project",
            title=None,
            card_layouts=[],
        )

    with pytest.raises(WhiteboardScopeError):
        manager.update_layout(
            cwd="",
            scope="project",
            title=None,
            card_layouts=[],
        )


def test_m16_update_layout_project_first_write_creates_meta_json(
    manager: WhiteboardManager, tmp_path: Path
) -> None:
    """R2 #1 修复回归测试。

    用户先调 PATCH layout（未先 create_card）时，project workspace 也要落 meta.json
    备份原始 cwd——否则违反 README 风险表"每个 project 子目录首次写时落盘 meta.json"
    承诺。
    """
    import json

    cwd = "/Users/kid/proj/layout-first"
    encoded = encode_project_dir(cwd)
    project_root = tmp_path / "projects" / encoded
    meta_path = project_root / "meta.json"

    # 前置断言：project 子目录尚不存在
    assert not project_root.exists()

    # 直接通过 update_layout 首写 project workspace（不走 create_card）
    manager.update_layout(
        cwd=cwd,
        scope="project",
        title="My Project Board",
        card_layouts=[],
    )

    # meta.json 必须存在 + 内容 = 原始 cwd
    assert meta_path.exists(), "首写 layout 时未落 meta.json"
    meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta_data["cwd"] == cwd


# ---------------------------------------------------------------------------
# E1：encode_project_dir 确定性 — 同一 cwd 两次 create 落同一 project workspace
# ---------------------------------------------------------------------------


def test_e1_same_cwd_two_creates_share_workspace(
    manager: WhiteboardManager, tmp_path: Path
) -> None:
    cwd = "/Users/kid/proj/a"
    manager.create_card(cwd=cwd, scope="project", title="C1", content="c1")
    manager.create_card(cwd=cwd, scope="project", title="C2", content="c2")

    encoded = encode_project_dir(cwd)
    project_ws = tmp_path / "projects" / encoded
    cards_dir = project_ws / "cards"

    md_files = list(cards_dir.glob("*.md"))
    assert len(md_files) == 2

    # encode_project_dir 自身确定性
    assert encode_project_dir(cwd) == encoded

    # 只有一个 projects 子目录被创建
    project_subdirs = [p for p in (tmp_path / "projects").iterdir() if p.is_dir()]
    assert len(project_subdirs) == 1


# ---------------------------------------------------------------------------
# E2：不同 cwd 落不同 project 子目录，互相 load 不污染
# ---------------------------------------------------------------------------


def test_e2_different_cwds_isolated(manager: WhiteboardManager, tmp_path: Path) -> None:
    cwd_a = "/Users/kid/proj/a"
    cwd_b = "/Users/kid/proj/b"

    card_a = manager.create_card(cwd=cwd_a, scope="project", title="A1", content="a")
    card_b = manager.create_card(cwd=cwd_b, scope="project", title="B1", content="b")

    encoded_a = encode_project_dir(cwd_a)
    encoded_b = encode_project_dir(cwd_b)
    assert encoded_a != encoded_b

    ws_a = tmp_path / "projects" / encoded_a
    ws_b = tmp_path / "projects" / encoded_b
    assert ws_a.is_dir() and ws_b.is_dir()
    assert ws_a != ws_b

    # 从 cwd_a 视角 load 只看到 A1（外加任意 global，本用例没建 global）
    snap_a = manager.load(cwd=cwd_a)
    a_ids = [c.record.id for c in snap_a.cards if c.scope == "project"]
    assert a_ids == [card_a.record.id]
    assert card_b.record.id not in a_ids

    # 从 cwd_b 视角 load 只看到 B1
    snap_b = manager.load(cwd=cwd_b)
    b_ids = [c.record.id for c in snap_b.cards if c.scope == "project"]
    assert b_ids == [card_b.record.id]
    assert card_a.record.id not in b_ids


# ---------------------------------------------------------------------------
# E3：encode_project_dir 格式契约 — 可读 basename + sha256[:8]
# ---------------------------------------------------------------------------


def test_e3_encode_project_dir_format() -> None:
    """目录名格式：``<basename-slug>-<sha256[:8]>``。

    覆盖：
    - 普通 ASCII 路径 → basename 保留
    - 非 ASCII basename → 退化为 ``project``
    - 同 basename 不同路径 → hash 区分（不撞）
    - 确定性：同一 cwd 永远算出同一目录名
    """
    import hashlib

    # 1. 普通路径：basename 保留
    cwd_a = "/Users/kid/proj/kongming-agent"
    expected_hash_a = hashlib.sha256(cwd_a.encode("utf-8")).hexdigest()[:8]
    assert encode_project_dir(cwd_a) == f"kongming-agent-{expected_hash_a}"

    # 2. 同 basename 不同路径 → hash 区分
    cwd_b = "/Users/kid/work/kongming-agent"
    expected_hash_b = hashlib.sha256(cwd_b.encode("utf-8")).hexdigest()[:8]
    encoded_a = encode_project_dir(cwd_a)
    encoded_b = encode_project_dir(cwd_b)
    assert encoded_a != encoded_b
    assert encoded_b == f"kongming-agent-{expected_hash_b}"

    # 3. 非 ASCII basename → 退化为 "project"
    cwd_unicode = "/Users/kid/项目/喵🐱"
    encoded_unicode = encode_project_dir(cwd_unicode)
    assert encoded_unicode.startswith("project-")
    expected_hash_unicode = hashlib.sha256(cwd_unicode.encode("utf-8")).hexdigest()[:8]
    assert encoded_unicode == f"project-{expected_hash_unicode}"

    # 4. 确定性：多次调用同一 cwd 算出同一目录名
    assert encode_project_dir(cwd_a) == encode_project_dir(cwd_a)
