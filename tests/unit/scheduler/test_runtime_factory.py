"""v0.3 cron-delivery M2：``build_cron_execution_bridge`` 装配的 session
backend 必须跟随 ``config.session.backend`` —— 不能始终回退 InMemorySession。

历史 bug（cron-delivery-v0.3 M2 修复目标）：

- v0.2 ``build_cron_execution_bridge`` 调 ``NativeRuntime.build`` 时**没传**
  ``session_factory``；
- ``NativeRuntime.build`` 的 fallback 是 ``InMemorySession``；
- 结果：即便用户配置 ``backend: file``，cron fresh session 仍走内存，
  ``.kongming/sessions/sched-*`` 目录从来不创建，跑完即丢。

本文件锁定修复后的不变量。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from infrastructure.config.models import Config, ModelConfig
from scheduler.runtime_factory import build_cron_execution_bridge
from scheduler.store import Store


def _make_config_with_file_backend(tmp_path: Path) -> Config:
    cfg = Config(
        model=ModelConfig(
            name="fake-model",
            base_url="http://127.0.0.1:11434",
            api_key="sk-fake",
        )
    )
    cfg.session.backend = "file"
    cfg.session.file_store_path = str(tmp_path / "sessions")
    cfg.scheduler.enabled = False  # 不启动 ticker；本测试只测装配
    return cfg


@pytest.mark.asyncio
async def test_cron_runtime_uses_file_backend_when_configured(tmp_path: Path) -> None:
    """``cfg.session.backend='file'`` → cron 装配出来的 session_factory 必须
    返回 :class:`FileSession`，而不是 InMemorySession。"""
    cfg = _make_config_with_file_backend(tmp_path)
    store = Store(tmp_path / "cron")

    runtime, _bridge = build_cron_execution_bridge(cfg, store)
    try:
        session = runtime.session_factory("sched-task-test-abc123")
        type_name = type(session).__name__
        # 修复后必须是 FileSession；修复前是 InMemorySession
        assert type_name == "FileSession", (
            f"expected FileSession when cfg.session.backend='file', "
            f"got {type_name} (cron fresh session 不落盘 bug 未修)"
        )
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_cron_runtime_uses_memory_backend_when_configured(tmp_path: Path) -> None:
    """``cfg.session.backend='memory'`` → 仍然返回 InMemorySession。"""
    cfg = Config(
        model=ModelConfig(
            name="fake-model",
            base_url="http://127.0.0.1:11434",
            api_key="sk-fake",
        )
    )
    cfg.session.backend = "memory"
    cfg.scheduler.enabled = False

    store = Store(tmp_path / "cron")
    runtime, _bridge = build_cron_execution_bridge(cfg, store)
    try:
        session = runtime.session_factory("sched-task-mem")
        assert type(session).__name__ == "InMemorySession"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_cron_session_persists_messages_to_disk(tmp_path: Path) -> None:
    """端到端冒烟：file backend 下 cron session.append 后磁盘上出现
    ``.kongming/sessions/sched-task-*/<sid>.jsonl``。"""
    from core.message import Message

    cfg = _make_config_with_file_backend(tmp_path)
    store = Store(tmp_path / "cron")

    runtime, _bridge = build_cron_execution_bridge(cfg, store)
    try:
        sid = "sched-task-persist-test"
        session = runtime.session_factory(sid)
        await session.append(Message(role="user", content="ping"))
        await session.append(Message(role="assistant", content="pong"))

        session_dir = tmp_path / "sessions" / sid
        assert session_dir.is_dir(), f"session dir {session_dir} 不存在；fresh session 未落盘"
        manifest = session_dir / "manifest.json"
        messages_jsonl = session_dir / f"{sid}.jsonl"
        assert manifest.is_file(), "manifest.json 缺失"
        assert messages_jsonl.is_file(), "messages jsonl 缺失"

        # 验证内容包含两条消息（每行一条）
        lines = [
            line for line in messages_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert len(lines) == 2
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_cron_runtime_accepts_external_session_factory(tmp_path: Path) -> None:
    """允许调用方（cli/web）传入自定义 session_factory，覆盖默认行为。

    用例：cli/web 已有自己装配好的 session_factory（含正确的 bootstrap），
    可直接复用而不必让 cron runtime_factory 再造一个。
    """
    cfg = _make_config_with_file_backend(tmp_path)
    store = Store(tmp_path / "cron")

    received_sids: list[str] = []

    def custom_factory(sid: str):  # type: ignore[no-untyped-def]
        received_sids.append(sid)
        from core.session import InMemorySession

        return InMemorySession(session_id=sid)

    runtime, _bridge = build_cron_execution_bridge(cfg, store, session_factory=custom_factory)
    try:
        s = runtime.session_factory("sched-task-custom")
        assert received_sids == ["sched-task-custom"]
        assert type(s).__name__ == "InMemorySession"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_cron_runtime_threads_dispatcher_to_bridge(tmp_path: Path) -> None:
    """v0.3 cron-delivery M3：``build_cron_execution_bridge(dispatcher=...)``
    必须把 dispatcher 透传给 ExecutionBridge.__init__。

    没有这条透传，cli/web 装配方就无法把 M4/M5 的 sink 注入到 bridge ——
    形成"中间环不通"，cron 触发的 final_message 不会路由到任何 channel。
    """
    from scheduler.delivery import DeliveryDispatcher

    cfg = _make_config_with_file_backend(tmp_path)
    store = Store(tmp_path / "cron")
    dispatcher = DeliveryDispatcher()

    runtime, bridge = build_cron_execution_bridge(cfg, store, dispatcher=dispatcher)
    try:
        # 透传不变量：bridge._dispatcher 是同一个对象
        assert bridge._dispatcher is dispatcher
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_cron_runtime_dispatcher_default_none_keeps_v02_behavior(
    tmp_path: Path,
) -> None:
    """``dispatcher`` 不传时 → bridge._dispatcher is None；
    bridge.execute 走 v0.2 路径不调投递（保持向后兼容）。"""
    cfg = _make_config_with_file_backend(tmp_path)
    store = Store(tmp_path / "cron")

    runtime, bridge = build_cron_execution_bridge(cfg, store)
    try:
        assert bridge._dispatcher is None
    finally:
        await runtime.aclose()


def test_cron_runtime_forwards_tools_and_enabled_names(tmp_path: Path) -> None:
    """Explicit tools wiring should be forwarded into NativeRuntime.build."""
    cfg = _make_config_with_file_backend(tmp_path)
    store = Store(tmp_path / "cron")
    fake_runtime = type(
        "FakeRuntime",
        (),
        {
            "runner": object(),
            "llm": object(),
            "tools": object(),
            "enabled_tool_names": ["read_file"],
            "approval": object(),
            "session_factory": object(),
            "agent_spec": object(),
        },
    )()
    tools = {"read_file": object()}

    with patch(
        "runtime_assembly.native_runtime.NativeRuntime.build",
        return_value=fake_runtime,
    ) as mock_build:
        _runtime, _bridge = build_cron_execution_bridge(
            cfg,
            store,
            tools=tools,
            enabled_tool_names=["read_file"],
        )

    _, kwargs = mock_build.call_args
    assert kwargs["tools"] is tools
    assert kwargs["enabled_tool_names"] == ["read_file"]


@pytest.mark.asyncio
async def test_cron_runtime_bridge_retains_scheduler_approval_config(tmp_path: Path) -> None:
    """ExecutionBridge should retain scheduler approval config from Config."""
    cfg = _make_config_with_file_backend(tmp_path)
    cfg.scheduler.approval.allow_write_file_create_in_cwd = False
    store = Store(tmp_path / "cron")

    runtime, bridge = build_cron_execution_bridge(cfg, store)
    try:
        assert bridge._base_config is cfg
        assert bridge._base_config.scheduler.approval.allow_write_file_create_in_cwd is False
    finally:
        await runtime.aclose()
