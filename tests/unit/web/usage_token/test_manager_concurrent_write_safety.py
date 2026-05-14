"""并发写盘安全 + DeriveProvider 集成单测。

⚠️ **关键回归保护**：上一版 ``fix-last-snapshot-persist`` 引入了
``set_last_assistant_usage`` / ``set_thread_model`` 的 fire-and-forget 写盘，
没拿 per-thread lock，跟 ``record_run_usage`` 在 lock 内的写盘竞争，导致
metadata.json 出现"两次 JSON 拼一起"的尾巴垃圾。

本套测试钉死：

1. ``set_last_assistant_usage`` N=20 路并发 + ``record_run_usage`` 一路串行：
   写盘只发生在 ``record_run_usage`` 路径（lock 内单写者），dictstore 始终是
   合法 schema，不会出现两次 dump 拼一起
2. DeriveProvider 注入路径：成功派生时 cumulative / last_snapshot 完全来自
   jsonl 真源，覆盖内存值；非 claude_code 通道 fallback 到 metadata 读盘
3. derive 异常时静默 fallback，主链路不受影响
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.unit.web.usage_token._fixtures import (
    InMemoryThreadMetadataIO,
    anthropic_raw,
)
from web.usage_token import UsageTokenManager
from web.usage_token._models import UsageTokenSnapshot

THREAD_ID = "thread-aaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# Fixture：dummy DeriveProvider 实现
# ---------------------------------------------------------------------------


class _FakeJsonlPathProvider:
    """符合 DeriveProvider 协议的测试桩。"""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.calls = 0

    async def get_claude_jsonl_path(self, thread_id: str) -> Path | None:
        self.calls += 1
        return self.path


class _ExplodingProvider:
    """每次调都抛异常（验证 manager 静默 fallback）。"""

    async def get_claude_jsonl_path(self, thread_id: str) -> Path | None:
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoConcurrentWriteCorruption:
    """**核心回归**：N 路并发 ``set_last_assistant_usage`` 不会写盘。"""

    @pytest.mark.asyncio
    async def test_set_last_assistant_usage_does_not_write_disk(self, tmp_path: Path) -> None:
        """N 路并发 ``set_last_assistant_usage`` 后，io.snapshot_store 永远是空的。

        这是 v0.1 治本改动的核心保证：``set_last_assistant_usage`` 纯内存，
        无 fire-and-forget 写盘 → 没有竞争写者。
        """
        io = InMemoryThreadMetadataIO()
        manager = UsageTokenManager(
            home=tmp_path,
            thread_metadata_io=io,
        )

        # 20 路并发 set_last_assistant_usage
        async def one_call(i: int) -> None:
            manager.set_last_assistant_usage(
                THREAD_ID,
                channel="anthropic",
                raw_payload=anthropic_raw(input_tokens=i, output_tokens=i * 2),
                model="claude-opus-4",
            )

        await asyncio.gather(*(one_call(i) for i in range(20)))
        # 给可能的后台任务一个 tick（不应该有任何后台任务）
        await asyncio.sleep(0)

        # **关键断言**：盘上**没有**写过任何 snapshot / model_name
        assert io.snapshot_store.get(THREAD_ID) is None
        assert io.model_store.get(THREAD_ID) is None
        # 但内存里有最后一次写入
        assert THREAD_ID in manager._last_snapshot
        assert manager._thread_models[THREAD_ID] == "claude-opus-4"

    @pytest.mark.asyncio
    async def test_record_run_usage_persists_in_lock_no_corruption(self, tmp_path: Path) -> None:
        """record_run_usage 串行，多次调用后 cumulative_store / model_store 是合法
        schema（json roundtrip OK）。"""
        io = InMemoryThreadMetadataIO()
        manager = UsageTokenManager(home=tmp_path, thread_metadata_io=io)

        # 5 次 record_run_usage 并发 —— per-thread lock 串行化
        async def one_record(i: int) -> None:
            await manager.record_run_usage(
                THREAD_ID,
                channel="anthropic",
                raw_payload=anthropic_raw(input_tokens=10, output_tokens=5),
                turn=i,
                run_id=f"run-{i}",
                model="claude-opus-4",
            )

        await asyncio.gather(*(one_record(i) for i in range(5)))

        # 5 轮累加：input=50, output=25
        stored = io.store[THREAD_ID]
        assert stored["channel"] == "anthropic"
        assert stored["input_tokens"] == 50
        assert stored["output_tokens"] == 25
        # snapshot / model 也都写到了 io
        snap = io.snapshot_store[THREAD_ID]
        assert snap["channel"] == "anthropic"
        assert io.model_store[THREAD_ID] == "claude-opus-4"
        # json roundtrip OK
        json.dumps(stored)
        json.dumps(snap)


class TestDeriveProviderIntegration:
    """DeriveProvider 注入路径：jsonl 派生覆盖 metadata 读盘。"""

    @pytest.mark.asyncio
    async def test_summary_uses_derived_cumulative_when_provider_returns_path(
        self, tmp_path: Path
    ) -> None:
        """provider 返回有效 jsonl path → get_thread_summary 用派生数据。"""
        # 准备 jsonl 文件
        jsonl_path = tmp_path / "test.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as fh:
            for _ in range(3):
                fh.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "sessionId": "s1",
                            "message": {
                                "model": "claude-opus-4",
                                "usage": {
                                    "input_tokens": 10,
                                    "output_tokens": 20,
                                    "cache_read_input_tokens": 100,
                                    "cache_creation_input_tokens": 5,
                                },
                            },
                        }
                    )
                    + "\n"
                )

        io = InMemoryThreadMetadataIO()
        # 故意先在 metadata 写一份"坏"数据（input=999）；派生应该 override 它
        io.store[THREAD_ID] = {
            "channel": "anthropic",
            "input_tokens": 999,
            "output_tokens": 999,
            "cache_read_input_tokens": 999,
            "cache_creation_input_tokens": 999,
        }

        manager = UsageTokenManager(
            home=tmp_path,
            thread_metadata_io=io,
            model_context_windows={"claude-opus-4": 1_000_000},
            derive_provider=_FakeJsonlPathProvider(jsonl_path),
        )

        summary = await manager.get_thread_summary(THREAD_ID)
        # 派生值（3 条 * 10/20/100/5）覆盖 metadata 里的 999
        assert summary.cumulative_input_tokens == 30
        assert summary.cumulative_output_tokens == 60
        assert summary.extras["cache_read_input_tokens"] == 300
        assert summary.extras["cache_creation_input_tokens"] == 15
        # last_run_context_usage = 最后一条的 input + cache_read + cache_creation = 10+100+5
        assert summary.last_run_context_usage == 115
        # model_name 来自 jsonl
        assert summary.model_name == "claude-opus-4"
        assert summary.model_context_window == 1_000_000

    @pytest.mark.asyncio
    async def test_summary_falls_back_when_provider_returns_none(self, tmp_path: Path) -> None:
        """provider 返回 None（非 claude_code 通道）→ fallback 读 metadata。"""
        io = InMemoryThreadMetadataIO()
        io.store[THREAD_ID] = {
            "channel": "anthropic",
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        io.model_store[THREAD_ID] = "claude-opus-4"

        manager = UsageTokenManager(
            home=tmp_path,
            thread_metadata_io=io,
            model_context_windows={"claude-opus-4": 1_000_000},
            derive_provider=_FakeJsonlPathProvider(None),  # 总返 None
        )

        summary = await manager.get_thread_summary(THREAD_ID)
        # fallback 路径读 metadata
        assert summary.cumulative_input_tokens == 100
        assert summary.cumulative_output_tokens == 50
        assert summary.model_name == "claude-opus-4"

    @pytest.mark.asyncio
    async def test_summary_falls_back_when_provider_raises(self, tmp_path: Path) -> None:
        """provider 抛异常 → manager 静默捕获，走 fallback。"""
        io = InMemoryThreadMetadataIO()
        io.store[THREAD_ID] = {
            "channel": "anthropic",
            "input_tokens": 7,
            "output_tokens": 3,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        manager = UsageTokenManager(
            home=tmp_path,
            thread_metadata_io=io,
            derive_provider=_ExplodingProvider(),
        )

        # 不应抛
        summary = await manager.get_thread_summary(THREAD_ID)
        assert summary.cumulative_input_tokens == 7

    @pytest.mark.asyncio
    async def test_summary_falls_back_when_provider_path_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        """provider 返回路径，但文件不存在 → derive 返 None → fallback。"""
        io = InMemoryThreadMetadataIO()
        manager = UsageTokenManager(
            home=tmp_path,
            thread_metadata_io=io,
            derive_provider=_FakeJsonlPathProvider(tmp_path / "nope.jsonl"),
        )

        summary = await manager.get_thread_summary(THREAD_ID)
        # 没数据 → 全零
        assert summary.cumulative_input_tokens == 0
        assert summary.cumulative_output_tokens == 0

    @pytest.mark.asyncio
    async def test_get_last_run_snapshot_uses_derive_too(self, tmp_path: Path) -> None:
        """get_last_run_snapshot 也走派生路径。"""
        jsonl_path = tmp_path / "test.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "s1",
                        "message": {
                            "model": "claude-opus-4",
                            "usage": {"input_tokens": 7, "output_tokens": 11},
                        },
                    }
                )
                + "\n"
            )

        io = InMemoryThreadMetadataIO()
        manager = UsageTokenManager(
            home=tmp_path,
            thread_metadata_io=io,
            derive_provider=_FakeJsonlPathProvider(jsonl_path),
        )

        snap = await manager.get_last_run_snapshot(THREAD_ID)
        assert snap is not None
        assert snap.input_tokens == 7
        assert snap.output_tokens == 11

    @pytest.mark.asyncio
    async def test_derive_syncs_caches_to_in_memory_state(self, tmp_path: Path) -> None:
        """派生成功时把 _last_snapshot / _thread_models 同步成 jsonl 真值。"""
        jsonl_path = tmp_path / "test.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "s1",
                        "message": {
                            "model": "claude-sonnet-4-5",
                            "usage": {
                                "input_tokens": 50,
                                "output_tokens": 100,
                                "cache_read_input_tokens": 200,
                            },
                        },
                    }
                )
                + "\n"
            )

        io = InMemoryThreadMetadataIO()
        manager = UsageTokenManager(
            home=tmp_path,
            thread_metadata_io=io,
            derive_provider=_FakeJsonlPathProvider(jsonl_path),
        )

        # 故意先写一个错误的内存 snapshot
        manager._last_snapshot[THREAD_ID] = UsageTokenSnapshot(
            channel="anthropic",
            input_tokens=999,
            output_tokens=999,
            extras={},
            context_usage=999,
            turn=0,
            run_id="",
        )

        await manager.get_thread_summary(THREAD_ID)
        # 派生覆盖内存
        assert manager._last_snapshot[THREAD_ID].input_tokens == 50
        assert manager._thread_models[THREAD_ID] == "claude-sonnet-4-5"


class TestNoDeriveProviderBackwardCompat:
    """``derive_provider=None``（构造时未注入）→ 完全走旧 metadata 读盘路径。"""

    @pytest.mark.asyncio
    async def test_summary_works_without_derive_provider(self, tmp_path: Path) -> None:
        io = InMemoryThreadMetadataIO()
        io.store[THREAD_ID] = {
            "channel": "anthropic",
            "input_tokens": 42,
            "output_tokens": 7,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        manager = UsageTokenManager(
            home=tmp_path,
            thread_metadata_io=io,
            # 不传 derive_provider
        )
        summary = await manager.get_thread_summary(THREAD_ID)
        assert summary.cumulative_input_tokens == 42
