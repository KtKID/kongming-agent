"""unit：core.session.InMemorySession 基础契约。

B6 / CR 报告 cr-report-20260424-202744.md。
"""

from __future__ import annotations

import pytest

from core.message import Message
from core.session import InMemorySession


@pytest.mark.asyncio
async def test_empty_history():
    s = InMemorySession("sid")
    assert await s.history() == []


@pytest.mark.asyncio
async def test_append_and_history_order():
    s = InMemorySession("sid")
    await s.append(Message.user("a"))
    await s.append(Message.user("b"))
    await s.append(Message.user("c"))
    hist = await s.history()
    assert [m.content for m in hist] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_history_returns_copy():
    s = InMemorySession("sid")
    await s.append(Message.user("a"))
    hist = await s.history()
    hist.append(Message.user("mutated"))
    # 内部未被污染
    assert [m.content for m in await s.history()] == ["a"]


@pytest.mark.asyncio
async def test_clear_resets_history():
    s = InMemorySession("sid")
    await s.append(Message.user("a"))
    await s.clear()
    assert await s.history() == []


@pytest.mark.asyncio
async def test_clear_is_idempotent():
    s = InMemorySession("sid")
    await s.clear()
    await s.clear()
    assert await s.history() == []


@pytest.mark.asyncio
async def test_clear_resets_run_index():
    s = InMemorySession("sid")
    assert await s.advance_run_index() == 1
    await s.clear()
    assert await s.advance_run_index() == 1


def test_auto_generated_session_id_when_not_provided():
    a = InMemorySession()
    b = InMemorySession()
    assert a.session_id.startswith("sess_")
    assert b.session_id.startswith("sess_")
    assert a.session_id != b.session_id


def test_explicit_session_id_is_respected():
    s = InMemorySession("my-sid")
    assert s.session_id == "my-sid"


@pytest.mark.asyncio
async def test_len_reflects_size():
    s = InMemorySession("sid")
    assert len(s) == 0
    await s.append(Message.user("a"))
    await s.append(Message.user("b"))
    assert len(s) == 2
    await s.clear()
    assert len(s) == 0


@pytest.mark.asyncio
async def test_advance_run_index_starts_at_one():
    s = InMemorySession("sid")
    assert await s.advance_run_index() == 1


@pytest.mark.asyncio
async def test_advance_run_index_monotonic():
    s = InMemorySession("sid")
    assert await s.advance_run_index() == 1
    assert await s.advance_run_index() == 2
    assert await s.advance_run_index() == 3


@pytest.mark.asyncio
async def test_advance_run_index_isolated_per_instance():
    a = InMemorySession("a")
    b = InMemorySession("b")
    assert await a.advance_run_index() == 1
    assert await b.advance_run_index() == 1
    assert await a.advance_run_index() == 2
    assert await b.advance_run_index() == 2


@pytest.mark.asyncio
async def test_get_run_count_starts_at_zero():
    """未 advance 过的 session，get_run_count 返回 0。"""
    s = InMemorySession("sid")
    assert await s.get_run_count() == 0


@pytest.mark.asyncio
async def test_get_run_count_matches_last_advance():
    """get_run_count 只读返回，与最后一次 advance_run_index 返回值相等，不递增。"""
    s = InMemorySession("sid")
    await s.advance_run_index()
    await s.advance_run_index()
    assert await s.get_run_count() == 2
    # 重复只读不递增
    assert await s.get_run_count() == 2
    assert await s.advance_run_index() == 3
    assert await s.get_run_count() == 3


@pytest.mark.asyncio
async def test_get_run_count_resets_on_clear():
    """clear 后 run_count 归零，get_run_count 反映归零状态。"""
    s = InMemorySession("sid")
    await s.advance_run_index()
    await s.advance_run_index()
    assert await s.get_run_count() == 2
    await s.clear()
    assert await s.get_run_count() == 0
