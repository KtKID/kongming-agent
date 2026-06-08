"""SessionManager 单测。"""

from __future__ import annotations

import asyncio

import pytest

from hosts.web.shared.session_manager import SessionManager


class _FakeWriter:
    def __init__(self, name: str = "writer") -> None:
        self.name = name


class _AttachableWriter:
    def __init__(self, target: object) -> None:
        self.target = target

    def attach_ws(self, new_target: object) -> None:
        self.target = new_target


async def test_register_and_get() -> None:
    sm = SessionManager()
    w = _FakeWriter("w1")
    record = await sm.register("sid-1", w)
    assert record.session_id == "sid-1"
    assert record.writer is w
    assert sm.get("sid-1") is record
    assert sm.is_active("sid-1") is True


async def test_register_existing_updates_writer() -> None:
    sm = SessionManager()
    w1 = _FakeWriter("w1")
    w2 = _FakeWriter("w2")
    await sm.register("sid", w1)
    record = await sm.register("sid", w2)
    assert record.writer is w2
    assert sm.get("sid") is record


async def test_unregister_returns_record_and_removes() -> None:
    sm = SessionManager()
    w = _FakeWriter()
    await sm.register("sid", w)
    record = await sm.unregister("sid")
    assert record is not None
    assert record.writer is w
    assert sm.is_active("sid") is False


async def test_unregister_unknown_returns_none() -> None:
    sm = SessionManager()
    assert await sm.unregister("nope") is None


async def test_replace_writer() -> None:
    sm = SessionManager()
    w1 = _FakeWriter("w1")
    w2 = _FakeWriter("w2")
    await sm.register("sid", w1)
    ok = await sm.replace_writer("sid", w2)
    assert ok is True
    assert sm.get("sid").writer is w2  # type: ignore[union-attr]


async def test_replace_writer_missing() -> None:
    sm = SessionManager()
    ok = await sm.replace_writer("nope", _FakeWriter())
    assert ok is False


async def test_replace_writer_rebinds_attachable_writer() -> None:
    sm = SessionManager()
    target1 = _FakeWriter("w1")
    target2 = _FakeWriter("w2")
    writer = _AttachableWriter(target1)
    await sm.register("sid", writer)
    ok = await sm.replace_writer("sid", target2)
    assert ok is True
    record = sm.get("sid")
    assert record is not None
    assert record.writer is writer
    assert writer.target is target2


async def test_rename_success() -> None:
    """rename: placeholder → 真实 SDK session_id 后保留 record 引用。"""
    sm = SessionManager()
    w = _FakeWriter()
    record = await sm.register("pending-X", w)
    ok = await sm.rename("pending-X", "real-uuid")
    assert ok is True
    assert sm.get("pending-X") is None
    assert sm.get("real-uuid") is record
    assert record.session_id == "real-uuid"


async def test_rename_unknown_returns_false() -> None:
    sm = SessionManager()
    ok = await sm.rename("nope", "anything")
    assert ok is False


async def test_rename_same_id_noop() -> None:
    sm = SessionManager()
    record = await sm.register("sid", _FakeWriter())
    ok = await sm.rename("sid", "sid")
    assert ok is True
    assert sm.get("sid") is record


async def test_rename_target_exists_returns_false() -> None:
    """避免覆盖已存在的 session_id。"""
    sm = SessionManager()
    await sm.register("a", _FakeWriter())
    await sm.register("b", _FakeWriter())
    ok = await sm.rename("a", "b")
    assert ok is False
    # 两个原 session 都仍存在
    assert sm.is_active("a")
    assert sm.is_active("b")


async def test_request_abort_sets_event_and_cancels_task() -> None:
    sm = SessionManager()
    w = _FakeWriter()
    record = await sm.register("sid", w)

    async def long_task() -> None:
        await asyncio.sleep(10)

    t = asyncio.create_task(long_task())
    record.query_task = t

    ok = await sm.request_abort("sid")
    assert ok is True
    assert record.abort_event.is_set() is True

    with pytest.raises(asyncio.CancelledError):
        await t


async def test_request_abort_missing_returns_false() -> None:
    sm = SessionManager()
    assert await sm.request_abort("nope") is False


async def test_list_active() -> None:
    sm = SessionManager()
    await sm.register("a", _FakeWriter())
    await sm.register("b", _FakeWriter())
    actives = sm.list_active()
    assert set(actives) == {"a", "b"}


async def test_concurrent_register_safe() -> None:
    """并发 register 不重复 / 不挂——asyncio.Lock 保护。"""
    sm = SessionManager()

    async def reg(i: int) -> None:
        await sm.register(f"sid-{i % 3}", _FakeWriter(f"w-{i}"))

    await asyncio.gather(*(reg(i) for i in range(30)))
    actives = sm.list_active()
    # 3 个唯一 sid（i % 3）
    assert set(actives) == {"sid-0", "sid-1", "sid-2"}


async def test_request_abort_no_query_task() -> None:
    """request_abort 在 query_task=None 时不抛错。"""
    sm = SessionManager()
    record = await sm.register("sid", _FakeWriter())
    assert record.query_task is None
    ok = await sm.request_abort("sid")
    assert ok is True
    assert record.abort_event.is_set() is True
