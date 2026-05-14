"""测试 last_run_snapshot / last_model_name 持久化 + 重启恢复。

⚠️ **v0.1 改动（usage-token-derive-from-jsonl）**：
``set_last_assistant_usage`` 和 ``set_thread_model`` **不再 fire-and-forget 写盘**
（消除并发写盘竞争）。只有 ``record_run_usage`` 在 per-thread lock 内串行写盘。
claude_code 通道还会通过 ``derive_provider`` 从 SDK jsonl 现场派生覆盖。

覆盖场景：
1. set_last_assistant_usage 后 → **不写盘**（仅内存）
2. record_run_usage 后 → io.snapshot_store / model_store 有对应 dict（lock 内串行）
3. 重启场景：新建 manager 实例 → get_thread_summary 从盘恢复 last_run_context_usage
4. set_thread_model 不写盘（防回归）
5. lazy load 命中（首次访问读盘 + 后续访问从内存）
6. schema 兼容：v8 文件缺 last_run_snapshot / last_model_name 不报错
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.web.usage_token._fixtures import (
    InMemoryThreadMetadataIO,
    anthropic_raw,
    openai_raw,
)
from web.usage_token import UsageTokenManager

THREAD_ID = "thread-cccccccccccc"

MODEL_WINDOWS = {
    "claude-opus-4": 1_000_000,
    "claude-sonnet-4": 200_000,
}


@pytest.fixture
def io() -> InMemoryThreadMetadataIO:
    return InMemoryThreadMetadataIO()


@pytest.fixture
def manager(tmp_path: Path, io: InMemoryThreadMetadataIO) -> UsageTokenManager:
    return UsageTokenManager(
        home=tmp_path,
        thread_metadata_io=io,
        model_context_windows=MODEL_WINDOWS,
    )


class TestSnapshotPersistOnSetLastAssistantUsage:
    @pytest.mark.asyncio
    async def test_snapshot_NOT_written_to_io(
        self, manager: UsageTokenManager, io: InMemoryThreadMetadataIO
    ) -> None:
        """**v0.1 防回归**：set_last_assistant_usage 不应写盘（纯内存）。

        消除 fire-and-forget 写盘竞争——claude_code 通道的 last_snapshot 通过
        ``derive_provider`` 从 jsonl 现场派生恢复，不需要 metadata 落盘。
        """
        raw = anthropic_raw(
            input_tokens=50_000,
            cache_read_input_tokens=400_000,
            output_tokens=2_000,
        )
        snapshot = manager.set_last_assistant_usage(THREAD_ID, channel="anthropic", raw_payload=raw)
        # 给 event loop 一个 tick——如果有 fire-and-forget 任务这里会跑
        import asyncio

        await asyncio.sleep(0)
        # 内存里有 snapshot
        assert snapshot is not None
        assert manager._last_snapshot[THREAD_ID].input_tokens == 50_000
        # 但盘上没有写入
        assert io.snapshot_store.get(THREAD_ID) is None


class TestSnapshotPersistOnRecordRunUsage:
    @pytest.mark.asyncio
    async def test_snapshot_written_to_io(
        self, manager: UsageTokenManager, io: InMemoryThreadMetadataIO
    ) -> None:
        """record_run_usage 后 snapshot_store 应有对应 dict。"""
        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(input_tokens=100, output_tokens=20),
            turn=1,
            run_id="run-1",
        )
        stored = io.snapshot_store.get(THREAD_ID)
        assert stored is not None
        assert stored["input_tokens"] == 100
        assert stored["output_tokens"] == 20
        assert stored["turn"] == 1
        assert stored["run_id"] == "run-1"


class TestModelNamePersist:
    @pytest.mark.asyncio
    async def test_set_thread_model_does_NOT_persist(
        self, manager: UsageTokenManager, io: InMemoryThreadMetadataIO
    ) -> None:
        """**v0.1 防回归**：set_thread_model 不应写盘（纯内存）。

        消除 fire-and-forget 写盘竞争。model_name 由 ``record_run_usage`` 在
        lock 内同步持久化，或由 ``derive_provider`` 从 jsonl 派生恢复。
        """
        import asyncio

        manager.set_thread_model(THREAD_ID, "claude-opus-4")
        await asyncio.sleep(0)
        # 内存里有
        assert manager._thread_models[THREAD_ID] == "claude-opus-4"
        # 盘上没有
        assert io.model_store.get(THREAD_ID) is None

    @pytest.mark.asyncio
    async def test_model_via_record_run_usage_persists(
        self, manager: UsageTokenManager, io: InMemoryThreadMetadataIO
    ) -> None:
        """record_run_usage(model=...) 自动注入并持久化 model_name。"""
        import asyncio

        await manager.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(input_tokens=100, output_tokens=20),
            turn=1,
            run_id="run-1",
            model="claude-sonnet-4",
        )
        await asyncio.sleep(0)
        assert io.model_store.get(THREAD_ID) == "claude-sonnet-4"


class TestRestartRecovery:
    """模拟重启：用同一个 io 创建新 manager 实例，验证 lazy load 恢复。"""

    @pytest.mark.asyncio
    async def test_snapshot_recovered_after_restart(
        self, tmp_path: Path, io: InMemoryThreadMetadataIO
    ) -> None:
        """重启后 get_thread_summary 从盘恢复 last_run_context_usage。"""
        # --- 第一个 manager 实例（模拟正常运行）---
        mgr1 = UsageTokenManager(
            home=tmp_path, thread_metadata_io=io, model_context_windows=MODEL_WINDOWS
        )
        await mgr1.record_run_usage(
            THREAD_ID,
            channel="anthropic",
            raw_payload=anthropic_raw(
                input_tokens=100_000,
                cache_read_input_tokens=400_000,
                output_tokens=200,
            ),
            turn=1,
            run_id="run-1",
            model="claude-opus-4",
        )
        import asyncio

        await asyncio.sleep(0)

        # 验证 io 有数据
        assert io.snapshot_store.get(THREAD_ID) is not None
        assert io.model_store.get(THREAD_ID) == "claude-opus-4"

        # --- 第二个 manager 实例（模拟重启：内存清零）---
        mgr2 = UsageTokenManager(
            home=tmp_path, thread_metadata_io=io, model_context_windows=MODEL_WINDOWS
        )
        # 内存确实是空的
        assert THREAD_ID not in mgr2._last_snapshot
        assert THREAD_ID not in mgr2._thread_models

        # get_thread_summary 触发 lazy load
        summary = await mgr2.get_thread_summary(THREAD_ID)
        # context = 100K + 400K = 500K → 恢复成功
        assert summary.last_run_context_usage == 500_000
        assert summary.model_name == "claude-opus-4"
        assert summary.model_context_window == 1_000_000
        assert summary.context_usage_pct == 50.0

    @pytest.mark.asyncio
    async def test_get_last_run_snapshot_recovered(
        self, tmp_path: Path, io: InMemoryThreadMetadataIO
    ) -> None:
        """重启后 get_last_run_snapshot 从盘恢复。"""
        mgr1 = UsageTokenManager(
            home=tmp_path, thread_metadata_io=io, model_context_windows=MODEL_WINDOWS
        )
        await mgr1.record_run_usage(
            THREAD_ID,
            channel="openai",
            raw_payload=openai_raw(input_tokens=200, output_tokens=50),
            turn=3,
            run_id="run-3",
        )

        mgr2 = UsageTokenManager(
            home=tmp_path, thread_metadata_io=io, model_context_windows=MODEL_WINDOWS
        )
        snap = await mgr2.get_last_run_snapshot(THREAD_ID)
        assert snap is not None
        assert snap.channel == "openai"
        assert snap.input_tokens == 200
        assert snap.output_tokens == 50
        assert snap.turn == 3
        assert snap.run_id == "run-3"


class TestLazyLoadCaching:
    @pytest.mark.asyncio
    async def test_lazy_load_only_reads_once(self, tmp_path: Path) -> None:
        """首次访问读盘，后续访问从内存——验证 io 方法不被重复调用。"""

        class CountingIO(InMemoryThreadMetadataIO):
            def __init__(self) -> None:
                super().__init__()
                self.snapshot_read_count = 0
                self.model_read_count = 0

            async def read_last_run_snapshot(self, thread_id: str) -> dict[str, Any] | None:
                self.snapshot_read_count += 1
                return await super().read_last_run_snapshot(thread_id)

            async def read_last_model_name(self, thread_id: str) -> str:
                self.model_read_count += 1
                return await super().read_last_model_name(thread_id)

        cio = CountingIO()
        # 预填数据（模拟上次运行落盘的数据）
        cio.snapshot_store[THREAD_ID] = {
            "channel": "anthropic",
            "input_tokens": 1000,
            "output_tokens": 100,
            "extras": {},
            "context_usage": 1000,
            "turn": 1,
            "run_id": "run-1",
        }
        cio.model_store[THREAD_ID] = "claude-opus-4"

        mgr = UsageTokenManager(
            home=tmp_path, thread_metadata_io=cio, model_context_windows=MODEL_WINDOWS
        )

        # 第一次 get_thread_summary → 触发 lazy load
        await mgr.get_thread_summary(THREAD_ID)
        assert cio.snapshot_read_count == 1
        assert cio.model_read_count == 1

        # 第二次 get_thread_summary → 不再读盘（内存已有）
        await mgr.get_thread_summary(THREAD_ID)
        assert cio.snapshot_read_count == 1  # 不变
        assert cio.model_read_count == 1  # 不变


class TestSchemaCompatibility:
    def test_v8_file_without_new_fields_loads_ok(self, tmp_path: Path) -> None:
        """v8 文件缺 last_run_snapshot / last_model_name → 加载不报错。"""
        import json

        from web.thread_metadata import (
            read_thread_metadata,
            thread_metadata_path,
        )

        tid = "thread-dddddddddddd"
        path = thread_metadata_path(tmp_path, tid)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 写一个 v8 文件，故意不含 last_run_snapshot / last_model_name
        v8_data = {
            "id": tid,
            "name": "test thread",
            "preset_id": "",
            "backend_kind": "claude_code",
            "claude_thread_id": "",
            "codex_thread_id": "",
            "cwd": "",
            "created_at": 1000000.0,
            "updated_at": 1000000.0,
            "message_count": 0,
            "cumulative_usage": None,
            "is_pinned": False,
            "schema_version": 8,
        }
        path.write_text(json.dumps(v8_data), encoding="utf-8")

        meta = read_thread_metadata(tmp_path, tid)
        assert meta is not None
        assert meta.last_run_snapshot is None
        assert meta.last_model_name == ""
        assert meta.schema_version == 8
