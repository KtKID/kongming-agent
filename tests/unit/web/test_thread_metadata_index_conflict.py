"""``_build_thread_metadata_index`` 冲突容忍单测。

历史 ``bind_claude_thread`` 1:1 守门有过若干 pre-existing 漏点，导致同一
``claude_thread_id`` 对应多个 thread metadata。原 ``{m.ctid: m for m in metas}``
dict 推导后者覆盖前者——``list_thread_metadata`` 按
``(is_pinned, updated_at) DESC`` 排序时，最优（pin+新）的反被最老的覆盖，
scanner 拿到的 title / archived 全错。

本测试用例钉死冲突场景下保留**首条**（排序最优）的行为。
"""

from __future__ import annotations

from web.routers.claude import _build_thread_metadata_index
from web.thread_metadata import ThreadMetadata


def _make_meta(
    *,
    tid: str,
    claude_thread_id: str,
    name: str,
    is_pinned: bool = False,
    updated_at: float = 1000.0,
) -> ThreadMetadata:
    return ThreadMetadata(
        id=tid,
        name=name,
        preset_id="",
        backend_kind="claude_code",
        claude_thread_id=claude_thread_id,
        cwd="/x",
        created_at=1.0,
        updated_at=updated_at,
        is_pinned=is_pinned,
    )


def test_empty_list_returns_empty_index() -> None:
    assert _build_thread_metadata_index([]) == {}


def test_skips_metas_without_claude_thread_id() -> None:
    m1 = _make_meta(tid="thread-aaaaaaaaaaa1", claude_thread_id="", name="generic")
    m2 = _make_meta(tid="thread-aaaaaaaaaaa2", claude_thread_id="ctid-1", name="claude")
    idx = _build_thread_metadata_index([m1, m2])
    assert set(idx) == {"ctid-1"}
    assert idx["ctid-1"].id == "thread-aaaaaaaaaaa2"


def test_no_conflict_one_per_key() -> None:
    m1 = _make_meta(tid="thread-aaaaaaaaaaa1", claude_thread_id="ctid-1", name="A")
    m2 = _make_meta(tid="thread-aaaaaaaaaaa2", claude_thread_id="ctid-2", name="B")
    idx = _build_thread_metadata_index([m1, m2])
    assert idx["ctid-1"].id == "thread-aaaaaaaaaaa1"
    assert idx["ctid-2"].id == "thread-aaaaaaaaaaa2"


def test_conflict_keeps_first_in_list_not_last() -> None:
    """ctid 冲突时保留**列表首条**（调用方传入排序首位 = 最优）。"""
    optimal = _make_meta(
        tid="thread-aaaaaaaaaaa1",
        claude_thread_id="ctid-dup",
        name="OPTIMAL pin+新",
        is_pinned=True,
        updated_at=2000.0,
    )
    suboptimal = _make_meta(
        tid="thread-aaaaaaaaaaa2",
        claude_thread_id="ctid-dup",
        name="SUBOPTIMAL 未 pin 较老",
        is_pinned=False,
        updated_at=1000.0,
    )
    # 模拟 list_thread_metadata 输出顺序：(is_pinned, updated_at) DESC
    idx = _build_thread_metadata_index([optimal, suboptimal])
    assert idx["ctid-dup"].name == "OPTIMAL pin+新"
    assert idx["ctid-dup"].id == "thread-aaaaaaaaaaa1"


def test_conflict_three_way_keeps_first() -> None:
    """三条同 ctid 也只保留首条。"""
    a = _make_meta(
        tid="thread-aaaaaaaaaaa1",
        claude_thread_id="ctid-dup",
        name="A pin+最新",
        is_pinned=True,
        updated_at=3000.0,
    )
    b = _make_meta(
        tid="thread-aaaaaaaaaaa2",
        claude_thread_id="ctid-dup",
        name="B 未pin 中间",
        updated_at=2000.0,
    )
    c = _make_meta(
        tid="thread-aaaaaaaaaaa3",
        claude_thread_id="ctid-dup",
        name="C 未pin 最老",
        updated_at=1000.0,
    )
    idx = _build_thread_metadata_index([a, b, c])
    assert idx["ctid-dup"].name == "A pin+最新"


def test_regression_dict_comprehension_would_lose_optimal() -> None:
    """**回归测试**：钉死旧 ``{m.ctid: m for m in metas}`` 行为已被废弃。

    如果 helper 退化回 dict 推导，本测试会 fail——后者覆盖前者，留下次优。
    """
    optimal = _make_meta(
        tid="thread-aaaaaaaaaaa1",
        claude_thread_id="ctid-dup",
        name="保留我",
        is_pinned=True,
        updated_at=9999.0,
    )
    losers = [
        _make_meta(
            tid=f"thread-aaaaaaaa{i:04x}",
            claude_thread_id="ctid-dup",
            name=f"丢掉 {i}",
            updated_at=1000.0 + i,
        )
        for i in range(5)
    ]
    metas = [optimal, *losers]
    idx = _build_thread_metadata_index(metas)
    assert idx["ctid-dup"].name == "保留我"
    # 旧 dict 推导会留下 losers[-1]
    assert idx["ctid-dup"].id == "thread-aaaaaaaaaaa1"
