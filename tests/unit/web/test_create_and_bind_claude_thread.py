"""``ThreadManager.create_and_bind_claude_thread`` 原子性单测。

claude-session-rename-archive-metadata-source 收尾增量：消除老 import_claude_session
端点 "find→不存在→create→bind" 两步非原子的 race window（曾让同 ctid 并发请求
产出多条 thread metadata 即"幽灵 thread"）。

覆盖：

1. 未绑定 ctid → 新建 thread + 完成 bind（imported=True）
2. 已绑定 ctid → 返回 existing thread（imported=False，不再多建）
3. **并发 N 次同 ctid → 只创建 1 个 thread**（核心 race 验证）
4. 并发不同 ctid → 各自独立建（lock 按 ctid 分桶）
5. claude_thread_id="" → ValueError
6. bind 阶段异常 → create_thread 写入的 metadata 被回滚（无幽灵）
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from web.threads.manager import (
    ClaudeThreadConflictError,
    ThreadManager,
)
from web.threads.metadata import list_thread_metadata


def _make_runtime_factory() -> Callable:
    """测试用 noop runtime factory（本测试只摸 metadata 不 boot cell）。"""

    async def factory(*args, **kwargs):
        raise NotImplementedError("test fixture does not boot cells")

    return factory


def _make_cfg() -> object:
    """最小可用 Config 实例。"""
    from config_loader import load_config

    # 测试用默认 config，本测试不依赖具体字段
    return load_config(Path("config/setting.yaml"))


def _make_tm(tmp_path: Path) -> ThreadManager:
    return ThreadManager(
        _make_cfg(),
        kongming_home=tmp_path,
        runtime_factory=_make_runtime_factory(),
    )


# ── 基本语义 ───────────────────────────────────────────────


async def test_imports_when_not_bound(tmp_path: Path) -> None:
    tm = _make_tm(tmp_path)
    meta, imported = await tm.create_and_bind_claude_thread(
        claude_thread_id="ctid-new",
        cwd="/x",
        name="新会话",
    )
    assert imported is True
    assert meta.claude_thread_id == "ctid-new"
    assert meta.cwd == "/x"
    assert meta.name == "新会话"
    assert meta.backend_kind == "claude_code"


async def test_returns_existing_when_already_bound(tmp_path: Path) -> None:
    tm = _make_tm(tmp_path)
    first, first_imported = await tm.create_and_bind_claude_thread(
        claude_thread_id="ctid-dup",
        cwd="/x",
        name="首次",
    )
    assert first_imported is True

    second, second_imported = await tm.create_and_bind_claude_thread(
        claude_thread_id="ctid-dup",
        cwd="/x",
        name="第二次（应被忽略）",
    )
    assert second_imported is False
    assert second.id == first.id
    assert second.name == "首次"  # name 不被第二次覆盖


async def test_empty_claude_thread_id_raises(tmp_path: Path) -> None:
    tm = _make_tm(tmp_path)
    with pytest.raises(ValueError, match="claude_thread_id must not be empty"):
        await tm.create_and_bind_claude_thread(
            claude_thread_id="",
            cwd="/x",
            name="x",
        )


# ── 核心：并发 race 验证 ───────────────────────────────────


async def test_concurrent_same_ctid_creates_only_one_thread(tmp_path: Path) -> None:
    """**根因测试**：N 个并发请求同 ctid → 只创建 1 个 thread metadata。

    这是 claude-session-rename-archive-metadata-source 调查时发现的核心 race：
    老 import_claude_session 两步非原子（find → create → bind），并发请求
    在 find 后、bind 前的窗口期都看不到对方 create 的占位，于是各自又 create
    了一份，留下"幽灵 thread"。新原子方法用 per-ctid asyncio.Lock 解决。
    """
    tm = _make_tm(tmp_path)
    N = 10

    async def import_once() -> tuple[str, bool]:
        meta, imported = await tm.create_and_bind_claude_thread(
            claude_thread_id="ctid-race",
            cwd="/x",
            name="并发首试",
        )
        return meta.id, imported

    results = await asyncio.gather(*(import_once() for _ in range(N)))

    # 1. 只有一次 imported=True（首个进临界区的）
    imported_count = sum(1 for _, imp in results if imp)
    assert imported_count == 1, f"应只有 1 次 imported=True，实际 {imported_count}"

    # 2. 所有结果返回同一个 thread.id
    thread_ids = {tid for tid, _ in results}
    assert len(thread_ids) == 1, f"应只产生 1 个 thread.id，实际 {thread_ids}"

    # 3. 盘上只有 1 条 thread metadata（核心断言）
    metas = list_thread_metadata(tmp_path)
    claude_metas = [m for m in metas if m.claude_thread_id == "ctid-race"]
    assert len(claude_metas) == 1, f"应只有 1 条 metadata，实际 {len(claude_metas)}"


async def test_concurrent_different_ctids_create_independently(tmp_path: Path) -> None:
    """并发不同 ctid → 各自独立建（lock 按 ctid 分桶不互相阻塞）。"""
    tm = _make_tm(tmp_path)

    async def import_ctid(ctid: str) -> bool:
        _, imported = await tm.create_and_bind_claude_thread(
            claude_thread_id=ctid,
            cwd="/x",
            name=f"thread-{ctid}",
        )
        return imported

    ctids = [f"ctid-{i}" for i in range(5)]
    results = await asyncio.gather(*(import_ctid(c) for c in ctids))

    assert all(results), "每个不同 ctid 应都 imported=True"
    metas = list_thread_metadata(tmp_path)
    bound_ctids = {m.claude_thread_id for m in metas if m.claude_thread_id}
    assert bound_ctids == set(ctids)


# ── 异常路径：bind 失败回滚 ────────────────────────────────


async def test_bind_failure_rolls_back_create(tmp_path: Path, monkeypatch) -> None:
    """bind_claude_thread 抛 ClaudeThreadConflictError → create_thread 的
    metadata 被 ``delete_thread`` 回滚，不留幽灵。

    模拟方式：mock 本次调用的 bind_claude_thread 直接抛冲突错。
    """
    tm = _make_tm(tmp_path)

    metas_before = list_thread_metadata(tmp_path)
    count_before = len(metas_before)

    # mock bind 直接抛冲突
    async def boom_bind(*args, **kwargs):
        raise ClaudeThreadConflictError("simulated race: ctid already bound by external writer")

    monkeypatch.setattr(tm, "bind_claude_thread", boom_bind)

    with pytest.raises(ClaudeThreadConflictError):
        await tm.create_and_bind_claude_thread(
            claude_thread_id="ctid-conflict",
            cwd="/x",
            name="本次尝试",
        )

    # bind 失败应回滚 create_thread 写入的 metadata，盘上 metadata 总数不变
    metas_after = list_thread_metadata(tmp_path)
    assert len(metas_after) == count_before, (
        f"bind 失败应回滚 create_thread，盘上 metadata 总数应不变 "
        f"({count_before})，实际 {len(metas_after)}"
    )
    # 且 ctid-conflict 对应的 metadata 一条也没留
    assert not any(m.claude_thread_id == "ctid-conflict" for m in metas_after)
