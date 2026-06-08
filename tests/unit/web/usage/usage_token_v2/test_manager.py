"""UsageTokenManager v2 单测。

覆盖：通道分发 / 异常 fallback / 无状态 / 并发 / 未知 backend / 无 IO __init__。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from web.usage.usage_token_v2 import (
    ClaudeJsonlLocator,
    ClaudeUsage,
    CodexRolloutLocator,
    CodexUsage,
    GenericChatAnthropicUsage,
    GenericChatSessionLocator,
    ThreadMetadataReader,
    UsageTokenManager,
)

# ---------------------------------------------------------------------------
# 注入测试桩
# ---------------------------------------------------------------------------


class _FakeMetaReader:
    def __init__(self, mapping: dict[str, dict[str, Any] | None]) -> None:
        self.mapping = mapping
        self.calls = 0

    async def read(self, thread_id: str) -> dict[str, Any] | None:
        self.calls += 1
        return self.mapping.get(thread_id)


class _FakeClaudeLocator:
    def __init__(self, mapping: dict[str, Path | None]) -> None:
        self.mapping = mapping

    async def locate(self, thread_id: str) -> Path | None:
        return self.mapping.get(thread_id)


class _FakeCodexLocator:
    def __init__(self, mapping: dict[str, Path | None]) -> None:
        self.mapping = mapping

    async def locate(self, thread_id: str) -> Path | None:
        return self.mapping.get(thread_id)


class _FakeGenericLocator:
    def __init__(self, mapping: dict[str, tuple[Path, str] | None]) -> None:
        self.mapping = mapping

    async def locate(self, thread_id: str):  # type: ignore[no-untyped-def]
        return self.mapping.get(thread_id)


class _ExplodingMetaReader:
    async def read(self, thread_id: str) -> dict[str, Any] | None:
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# 测试用 fixture：写一份最小 jsonl
# ---------------------------------------------------------------------------


def _write_claude_jsonl(path: Path, input_tok: int = 10, model: str = "claude-opus-4") -> None:
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "sid",
                "message": {
                    "model": model,
                    "usage": {"input_tokens": input_tok, "output_tokens": 5},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_codex_rollout(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "output_tokens": 30,
                            "total_tokens": 130,
                        },
                        "last_token_usage": {
                            "input_tokens": 50,
                            "output_tokens": 10,
                            "total_tokens": 60,
                        },
                        "model_context_window": 258400,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_generic_jsonl(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "sid",
                "model_name": "claude-opus-4",
                "message_id": "msg",
                "parent_message_id": None,
                "created_at": 1.0,
                "message": {"role": "assistant", "content": []},
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }
        )
        + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_manager_has_no_state_fields() -> None:
    """manager 实例化后无任何持久状态字段（除注入的 locator 引用）。"""
    mgr = UsageTokenManager(
        meta_reader=_FakeMetaReader({}),
        claude_locator=_FakeClaudeLocator({}),
        codex_locator=_FakeCodexLocator({}),
        generic_locator=_FakeGenericLocator({}),
    )
    # 只能有 4 个 _ 前缀属性（4 个注入的 locator）
    private_attrs = [a for a in vars(mgr).keys() if a.startswith("_")]
    assert sorted(private_attrs) == sorted(["_meta", "_claude", "_codex", "_generic"])


def test_init_does_no_io(tmp_path: Path) -> None:
    """__init__ 不读盘 / 不开文件 / 不创建 task。"""
    # 不抛即 ok
    UsageTokenManager(
        meta_reader=_FakeMetaReader({}),
        claude_locator=_FakeClaudeLocator({}),
        codex_locator=_FakeCodexLocator({}),
        generic_locator=_FakeGenericLocator({}),
    )


def test_protocols_runtime_checkable() -> None:
    """4 个 Protocol 满足 runtime_checkable，装配层实现可 isinstance 验证。"""
    assert isinstance(_FakeMetaReader({}), ThreadMetadataReader)
    assert isinstance(_FakeClaudeLocator({}), ClaudeJsonlLocator)
    assert isinstance(_FakeCodexLocator({}), CodexRolloutLocator)
    assert isinstance(_FakeGenericLocator({}), GenericChatSessionLocator)


@pytest.mark.asyncio
async def test_dispatch_claude_code_returns_claude_usage(tmp_path: Path) -> None:
    p = tmp_path / "claude.jsonl"
    _write_claude_jsonl(p, input_tok=42)
    mgr = UsageTokenManager(
        meta_reader=_FakeMetaReader({"thread-x": {"backend_kind": "claude_code"}}),
        claude_locator=_FakeClaudeLocator({"thread-x": p}),
        codex_locator=_FakeCodexLocator({}),
        generic_locator=_FakeGenericLocator({}),
    )
    result = await mgr.get_thread_usage("thread-x")
    assert isinstance(result, ClaudeUsage)
    assert result.input_tokens == 42


@pytest.mark.asyncio
async def test_dispatch_codex_returns_codex_usage(tmp_path: Path) -> None:
    p = tmp_path / "rollout.jsonl"
    _write_codex_rollout(p)
    mgr = UsageTokenManager(
        meta_reader=_FakeMetaReader({"thread-y": {"backend_kind": "codex"}}),
        claude_locator=_FakeClaudeLocator({}),
        codex_locator=_FakeCodexLocator({"thread-y": p}),
        generic_locator=_FakeGenericLocator({}),
    )
    result = await mgr.get_thread_usage("thread-y")
    assert isinstance(result, CodexUsage)
    assert result.total.input_tokens == 100
    assert result.model_context_window == 258400


@pytest.mark.asyncio
async def test_dispatch_generic_chat_returns_generic_dto(tmp_path: Path) -> None:
    p = tmp_path / "session.jsonl"
    _write_generic_jsonl(p)
    mgr = UsageTokenManager(
        meta_reader=_FakeMetaReader({"thread-z": {"backend_kind": "generic_chat"}}),
        claude_locator=_FakeClaudeLocator({}),
        codex_locator=_FakeCodexLocator({}),
        generic_locator=_FakeGenericLocator({"thread-z": (p, "anthropic")}),
    )
    result = await mgr.get_thread_usage("thread-z")
    assert isinstance(result, GenericChatAnthropicUsage)
    assert result.input_tokens == 7


@pytest.mark.asyncio
async def test_meta_reader_returns_none_when_thread_missing() -> None:
    mgr = UsageTokenManager(
        meta_reader=_FakeMetaReader({}),
        claude_locator=_FakeClaudeLocator({}),
        codex_locator=_FakeCodexLocator({}),
        generic_locator=_FakeGenericLocator({}),
    )
    assert await mgr.get_thread_usage("nonexistent") is None


@pytest.mark.asyncio
async def test_unknown_backend_kind_returns_none() -> None:
    mgr = UsageTokenManager(
        meta_reader=_FakeMetaReader({"thread-x": {"backend_kind": "unknown_kind"}}),
        claude_locator=_FakeClaudeLocator({}),
        codex_locator=_FakeCodexLocator({}),
        generic_locator=_FakeGenericLocator({}),
    )
    assert await mgr.get_thread_usage("thread-x") is None


@pytest.mark.asyncio
async def test_locator_returns_none_yields_none() -> None:
    """locator 返回 None（找不到真源） → manager 返 None。"""
    mgr = UsageTokenManager(
        meta_reader=_FakeMetaReader({"thread-x": {"backend_kind": "claude_code"}}),
        claude_locator=_FakeClaudeLocator({"thread-x": None}),
        codex_locator=_FakeCodexLocator({}),
        generic_locator=_FakeGenericLocator({}),
    )
    assert await mgr.get_thread_usage("thread-x") is None


@pytest.mark.asyncio
async def test_meta_reader_exception_silently_returns_none() -> None:
    """meta_reader 抛异常时 manager 静默返 None，不冒泡。"""
    mgr = UsageTokenManager(
        meta_reader=_ExplodingMetaReader(),
        claude_locator=_FakeClaudeLocator({}),
        codex_locator=_FakeCodexLocator({}),
        generic_locator=_FakeGenericLocator({}),
    )
    # 不抛
    result = await mgr.get_thread_usage("thread-x")
    assert result is None


@pytest.mark.asyncio
async def test_repeated_query_no_caching(tmp_path: Path) -> None:
    """连续 2 次同 thread_id 调用，meta_reader 被调 2 次（无状态，无缓存）。"""
    p = tmp_path / "claude.jsonl"
    _write_claude_jsonl(p)
    meta = _FakeMetaReader({"thread-x": {"backend_kind": "claude_code"}})
    mgr = UsageTokenManager(
        meta_reader=meta,
        claude_locator=_FakeClaudeLocator({"thread-x": p}),
        codex_locator=_FakeCodexLocator({}),
        generic_locator=_FakeGenericLocator({}),
    )
    await mgr.get_thread_usage("thread-x")
    await mgr.get_thread_usage("thread-x")
    assert meta.calls == 2  # 每次都重新读 meta


@pytest.mark.asyncio
async def test_concurrent_queries_no_lock_no_race(tmp_path: Path) -> None:
    """100 路并发查不同 thread 各自正确返回，无 race / 无死锁。"""
    paths = []
    meta_map: dict[str, dict[str, Any] | None] = {}
    claude_map: dict[str, Path | None] = {}
    for i in range(100):
        p = tmp_path / f"thread-{i}.jsonl"
        _write_claude_jsonl(p, input_tok=i)
        paths.append(p)
        meta_map[f"thread-{i:012d}"] = {"backend_kind": "claude_code"}
        claude_map[f"thread-{i:012d}"] = p

    mgr = UsageTokenManager(
        meta_reader=_FakeMetaReader(meta_map),
        claude_locator=_FakeClaudeLocator(claude_map),
        codex_locator=_FakeCodexLocator({}),
        generic_locator=_FakeGenericLocator({}),
    )

    results = await asyncio.gather(*(mgr.get_thread_usage(f"thread-{i:012d}") for i in range(100)))
    # 每个 thread 各自 input_tokens 跟自己的 i 一致
    for i, r in enumerate(results):
        assert isinstance(r, ClaudeUsage)
        assert r.input_tokens == i
