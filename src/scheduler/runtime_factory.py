"""scheduler v0.1 — cron 装配工厂。

把 :meth:`NativeRuntime.build` 的装配能力复用过来构造一个
:class:`scheduler.execution_bridge.ExecutionBridge`：cron run 走 fresh
session + 工具裁剪 + watchdog，但 LLM / safety / tool registry / session 工厂
等装配仍然走主流程，避免 cron 自己复制一份装配。

调用方典型流程：

    runtime, bridge = build_cron_execution_bridge(config, store)
    try:
        await tick(store, bridge)         # 单次扫
        # 或
        await run_ticker_loop(store, bridge, stop_event)
    finally:
        await runtime.aclose()            # 释放 provider httpx 连接池

边界：

- 不装配 :class:`Store`，由调用方传入（CLI / web app lifespan 自己持有）。
- 不持有 ``stop_event`` / ``ticker``：本工厂只负责"造 bridge"，循环编排留给调用方。
- 仅依赖 :mod:`executors.agent_runtime.native_runtime` 暴露出来的 properties，
  不直接读私有字段。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from config_loader.models import Config
from executors.agent_runtime.native_runtime import NativeRuntime
from scheduler.execution_bridge import ExecutionBridge
from scheduler.store import Store

if TYPE_CHECKING:
    from collections.abc import Callable

    from config_loader.models import LLMPresetConfig
    from context.session_bootstrap import SessionBootstrap
    from core.contracts import EventSink, Session
    from scheduler.delivery import DeliveryDispatcher


def _default_cron_session_factory(
    config: Config, bootstrap: SessionBootstrap | None
) -> Callable[[str], Session]:
    """v0.3 cron-delivery M2：根据 ``config.session.backend`` 装配
    cron fresh session 的 session_factory。

    历史 bug：v0.2 ``build_cron_execution_bridge`` 调 ``NativeRuntime.build``
    时**没传** ``session_factory``，被 fallback 成 ``InMemorySession``，
    导致 ``cfg.session.backend='file'`` 时 cron 跑的整个对话历史
    （system prompt + user input + LLM 回复 + tool calls）跑完即丢，
    磁盘上 ``.kongming/sessions/sched-*`` 目录从来不创建。

    本函数生成默认 factory：

    - ``backend='memory'`` → 仍走 ``InMemorySession``（与 v0.2 行为一致）
    - ``backend='sqlite' | 'file'`` → 走 ``context.build_session``，**让 cron
      fresh session 跟主 session 用同一种 backend**

    file backend 需要 ``SessionBootstrap``：调用方未传时构造一个最小 placeholder
    （cron 是独立 fresh run，bootstrap 元数据不必跟主 session 一致）。
    """
    from context import SessionBootstrap, build_session

    resolved_bootstrap = bootstrap or SessionBootstrap(
        agent_name="kongming-agent-cron",
        model_name=config.model.name,
        instruction_sources=[],
        instruction_text_hash="cron-fresh",
        created_at=time.time(),
        cwd=str(Path.cwd()),
    )

    def factory(session_id: str) -> Session:
        return build_session(config, session_id, bootstrap=resolved_bootstrap)

    return factory


def build_cron_execution_bridge(
    config: Config,
    store: Store,
    *,
    event_sinks: list[EventSink] | None = None,
    instructions: str | None = None,
    session_factory: Callable[[str], Session] | None = None,
    session_bootstrap: SessionBootstrap | None = None,
    dispatcher: DeliveryDispatcher | None = None,
    preset_map: dict[str, LLMPresetConfig] | None = None,
    trace_dir: Path | None = None,
) -> tuple[NativeRuntime, ExecutionBridge]:
    """装配 :class:`NativeRuntime` + :class:`ExecutionBridge`。

    Args:
        config: ``load_config(...)`` 返回的 :class:`Config`。
        store: cron 模块的文件 :class:`Store`。
        event_sinks: 注入到底层 runner / bridge 的 event sinks（trace 落盘等）。
            缺省空列表。
        instructions: 透传给 :meth:`NativeRuntime.build`，构造默认 AgentSpec
            的 system prompt。
        session_factory: cron fresh session 的工厂；显式传入时优先使用，未传
            则按 ``config.session.backend`` 自动装配（v0.3 修复 fresh session
            不落盘 bug）。CLI / Web 主路径已有自己的 session_factory，建议
            直接传入以共享 bootstrap 元数据。
        session_bootstrap: 仅在 ``session_factory`` 未传 + backend 为 file 时
            生效；用作 ``FileSession`` 的最小 bootstrap。未传则构造 placeholder。
        dispatcher: v0.3 cron-delivery M3：cron run 完成后投递路由器。``None``
            时 bridge 跑 v0.2 行为（不投递）；M4/M5 装配方传入实例后，cron
            触发的 final_message 才会按 ``task.delivery.channel`` 路由到
            web / cli sink。
        preset_map: v0.4 cron-thread-preset：per-task LLM preset 映射表。
            ``task.preset_id`` 命中 key 时 bridge 按 preset 构建独立 provider；
            ``None`` 时所有 task 用同一个默认 provider。

    Returns:
        ``(runtime, bridge)`` 二元组：
        - ``runtime``: 调用方需保留引用，最终 ``await runtime.aclose()`` 释放
          底层 httpx 连接池。
        - ``bridge``: 已就绪的 :class:`ExecutionBridge`，可直接被 ticker /
          手动 ``run`` 调用。
    """
    sinks = list(event_sinks or [])
    resolved_factory = session_factory or _default_cron_session_factory(config, session_bootstrap)
    runtime = NativeRuntime.build(
        config,
        event_sinks=sinks,
        instructions=instructions,
        session_factory=resolved_factory,
    )
    bridge = ExecutionBridge(
        runner=runtime.runner,
        llm=runtime.llm,
        tools=runtime.tools,
        enabled_tool_names=runtime.enabled_tool_names,
        inner_approval=runtime.approval,
        session_factory=runtime.session_factory,
        event_sinks=sinks,
        agent_spec=runtime.agent_spec,
        store=store,
        dispatcher=dispatcher,
        preset_map=preset_map,
        base_config=config,
        trace_dir=trace_dir,
    )
    return runtime, bridge


__all__ = ["build_cron_execution_bridge"]
