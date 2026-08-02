"""SessionEngine 历史任务级门户测试。

覆盖公共 read/append/seed 复用同一 session、非空目标拒绝，以及部分播种失败
后的原子清空。测试只替换 LLM 装配，不绕过 SessionEngine 门户。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.contracts import ProviderUsageSnapshot
from core.message import Message
from core.session import InMemorySession
from infrastructure.config.models import Config
from runtime_assembly.session_engine import SessionEngine
from sessions.file_session import FileSession
from sessions.session_bootstrap import SessionBootstrap


def _config() -> Config:
    """构造无需远端凭据的本地模型配置。"""
    return Config.model_validate({"model": {"preset_id": "local-gemma-4-e4b-it"}})


class _FailingAppendSession(InMemorySession):
    """在指定 append 次序抛错，并记录 rollback clear。"""

    def __init__(self, session_id: str, *, fail_at: int) -> None:
        super().__init__(session_id)
        self._fail_at = fail_at
        self.append_attempts = 0
        self.clear_calls = 0

    async def append(
        self,
        message: Message,
        *,
        usage: ProviderUsageSnapshot | None = None,
    ) -> None:
        """前 N-1 条正常写入，第 N 条抛出可诊断错误。"""
        del usage
        self.append_attempts += 1
        if self.append_attempts == self._fail_at:
            raise RuntimeError("injected seed append failure")
        await super().append(message)

    async def clear(self) -> None:
        """记录 rollback 并清空已写入前缀。"""
        self.clear_calls += 1
        await super().clear()


class _FailingFileSession(FileSession):
    """真实 FileSession 在指定 append 次序注入持久化失败。"""

    def __init__(
        self,
        session_id: str,
        bootstrap: SessionBootstrap,
        store_path: str,
        *,
        fail_at: int,
    ) -> None:
        super().__init__(session_id, bootstrap, store_path)
        self._fail_at = fail_at
        self.append_attempts = 0

    async def append(
        self,
        message: Message,
        *,
        usage: ProviderUsageSnapshot | None = None,
    ) -> None:
        """在第 fail_at 次写盘前抛错，其余调用走真实 JSONL append。"""
        self.append_attempts += 1
        if self.append_attempts == self._fail_at:
            raise OSError("injected file seed failure")
        await super().append(message, usage=usage)


async def test_public_read_and_append_reuse_cached_session(tmp_path: Path) -> None:
    """read/append 通过同一公开门户和同一 factory 实例推进历史。"""
    del tmp_path
    created: list[InMemorySession] = []

    def _factory(session_id: str) -> InMemorySession:
        session = InMemorySession(session_id)
        created.append(session)
        return session

    runtime = SessionEngine.build(_config(), session_factory=_factory)
    try:
        await runtime.append_session_message("thread-a", Message.user("hello"))
        history = await runtime.read_session_history("thread-a")

        assert history == [Message.user("hello")]
        assert len(created) == 1
    finally:
        await runtime.aclose()


async def test_file_session_seed_failure_removes_persisted_prefix(
    tmp_path: Path,
) -> None:
    """真实 FileSession 写入一条后失败，rollback 删除目录和 JSONL 前缀。"""
    bootstrap = SessionBootstrap(
        agent_name="root",
        model_name="fake",
        instruction_sources=["test"],
        instruction_text_hash="history-portal",
        instruction_text="history portal",
        created_at=1.0,
        cwd=str(tmp_path),
    )
    target = _FailingFileSession(
        "target-file",
        bootstrap,
        str(tmp_path / "sessions"),
        fail_at=2,
    )
    runtime = SessionEngine.build(
        _config(),
        session_factory=lambda _session_id: target,
    )
    try:
        with pytest.raises(OSError, match="injected file seed failure"):
            await runtime.seed_empty_session_history(
                "target-file",
                [Message.user("one"), Message.assistant("two")],
            )

        assert target.append_attempts == 2
        assert await target.history() == []
        assert (tmp_path / "sessions" / "target-file").exists() is False
    finally:
        await runtime.aclose()


async def test_seed_rejects_nonempty_target_without_mutation() -> None:
    """目标已有历史时拒绝播种并保留原内容。"""
    runtime = SessionEngine.build(_config())
    try:
        await runtime.append_session_message("target", Message.user("existing"))

        with pytest.raises(ValueError, match="target session history must be empty"):
            await runtime.seed_empty_session_history(
                "target",
                [Message.user("new")],
            )

        assert await runtime.read_session_history("target") == [Message.user("existing")]
    finally:
        await runtime.aclose()


async def test_seed_partial_failure_clears_target_and_reraises_original() -> None:
    """第 N 条 append 失败后清空前缀并重新抛出原始错误。"""
    target = _FailingAppendSession("target", fail_at=2)
    runtime = SessionEngine.build(
        _config(),
        session_factory=lambda _session_id: target,
    )
    messages = [
        Message.user("one"),
        Message.assistant("two"),
        Message.user("three"),
    ]
    try:
        with pytest.raises(RuntimeError, match="injected seed append failure"):
            await runtime.seed_empty_session_history("target", messages)

        assert target.append_attempts == 2
        assert target.clear_calls == 1
        assert await target.history() == []
    finally:
        await runtime.aclose()
