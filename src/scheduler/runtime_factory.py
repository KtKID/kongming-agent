"""scheduler v0.1 — cron 装配工厂。

把 :meth:`SessionEngine.build` 的装配能力复用过来构造一个
:class:`application.scheduled_runs.execution_bridge.ExecutionBridge`：cron run 走 fresh
session + 工具裁剪 + watchdog，但 LLM / safety / tool registry / session 工厂
等装配仍然走主流程，避免 cron 自己复制一份装配。

生产调用方典型流程：

    runtime, manager = build_scheduled_run_manager(config, store)
    try:
        await tick(store, manager)
        await run_ticker_loop(store, manager, stop_event)
    finally:
        await manager.aclose()
        await runtime.aclose()

边界：

- 不装配 :class:`Store`，由调用方传入（CLI / web app lifespan 自己持有）。
- 不持有 ``stop_event`` / ``ticker``：本工厂负责执行 plan 和 live Manager 装配，
  循环编排留给调用方。
- 仅依赖 :mod:`runtime_assembly.session_engine` 暴露出来的 properties，
  不直接读私有字段。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from application.scheduled_runs.execution_bridge import ExecutionBridge
from application.scheduled_runs.manager import ScheduledRunManager
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.model_provider_catalog import ResolvedModelConfig
from infrastructure.config.models import Config
from runtime_assembly.session_engine import SessionEngine
from scheduler.store import Store

if TYPE_CHECKING:
    from collections.abc import Callable

    from application.scheduled_runs.manager import ScheduledRunDispatcherFactory
    from core.agent_spec import AgentSpec
    from core.contracts import ApprovalProvider, EventSink, Session, Tool, ToolLookup
    from scheduler.delivery import DeliveryDispatcher, RunLifecycleSink
    from scheduler.domain import ScheduledTask
    from sessions.session_bootstrap import SessionBootstrap


def _default_cron_session_factory(
    config: Config,
    bootstrap: SessionBootstrap | None,
    *,
    model_name: str,
    default_cwd: str | None = None,
) -> Callable[[str], Session]:
    """v0.3 cron-delivery M2：根据 ``config.session.backend`` 装配
    cron fresh session 的 session_factory。

    历史 bug：v0.2 ``build_cron_execution_bridge`` 调 ``SessionEngine.build``
    时**没传** ``session_factory``，被 fallback 成 ``InMemorySession``，
    导致 ``cfg.session.backend='file'`` 时 cron 跑的整个对话历史
    （system prompt + user input + LLM 回复 + tool calls）跑完即丢，
    磁盘上 ``.kongming/sessions/sched-*`` 目录从来不创建。

    本函数生成默认 factory：

    - ``backend='memory'`` → 仍走 ``InMemorySession``（与 v0.2 行为一致）
    - ``backend='sqlite' | 'file'`` → 走 ``sessions.build_session``，**让 cron
      fresh session 跟主 session 用同一种 backend**

    file backend 需要 ``SessionBootstrap``：调用方未传时构造一个最小 placeholder
    （cron 是独立 fresh run，bootstrap 元数据不必跟主 session 一致）。
    """
    from sessions import build_session

    def factory(session_id: str) -> Session:
        resolved_bootstrap = _build_cron_session_bootstrap(
            config,
            bootstrap,
            model_name=model_name,
            default_cwd=default_cwd,
        )
        return build_session(config, session_id, bootstrap=resolved_bootstrap)

    return factory


def _default_cron_session_factory_for_agent(
    config: Config,
    bootstrap: SessionBootstrap | None,
    *,
    default_cwd: str | None = None,
) -> Callable[[str, AgentSpec], Session]:
    from sessions import build_session

    def factory(session_id: str, agent_spec: AgentSpec) -> Session:
        resolved_bootstrap = _build_cron_session_bootstrap(
            config,
            bootstrap,
            model_name=agent_spec.default_model,
            default_cwd=default_cwd,
        )
        return build_session(config, session_id, bootstrap=resolved_bootstrap)

    return factory


def _build_cron_session_bootstrap(
    config: Config,
    bootstrap: SessionBootstrap | None,
    *,
    model_name: str,
    default_cwd: str | None = None,
) -> SessionBootstrap:
    from sessions import SessionBootstrap

    if bootstrap is not None:
        return replace(bootstrap, model_name=model_name, created_at=time.time())
    return SessionBootstrap(
        agent_name="kongming-agent-cron",
        model_name=model_name,
        instruction_sources=[],
        instruction_text_hash="cron-fresh",
        created_at=time.time(),
        cwd=default_cwd or str(Path.cwd()),
    )


def _metadata_cwd_string(metadata: Mapping[str, Any]) -> str | None:
    raw = metadata.get("cwd")
    if isinstance(raw, Path):
        return str(raw.expanduser().resolve(strict=False))
    if isinstance(raw, str) and raw.strip():
        return str(Path(raw).expanduser().resolve(strict=False))
    return None


def build_cron_execution_bridge(
    config: Config,
    store: Store,
    *,
    event_sinks: list[EventSink] | None = None,
    tools: ToolLookup | Mapping[str, Tool] | None = None,
    enabled_tool_names: list[str] | None = None,
    instructions: str | None = None,
    session_factory: Callable[[str], Session] | None = None,
    session_bootstrap: SessionBootstrap | None = None,
    dispatcher: DeliveryDispatcher | None = None,
    lifecycle_sink: RunLifecycleSink | None = None,
    interactive_approval_factory: (
        Callable[[ScheduledTask], ApprovalProvider | None] | None
    ) = None,
    tool_context_metadata: Mapping[str, Any] | None = None,
    tool_context_metadata_factory: (
        Callable[[ScheduledTask], Mapping[str, Any] | None] | None
    ) = None,
    model_catalog_manager: ModelCatalogManager | None = None,
    resolved_model: ResolvedModelConfig | None = None,
    trace_dir: Path | None = None,
) -> tuple[SessionEngine, ExecutionBridge]:
    """装配 :class:`SessionEngine` + :class:`ExecutionBridge`。

    Args:
        config: ``load_config(...)`` 返回的 :class:`Config`。
        store: cron 模块的文件 :class:`Store`。
        event_sinks: 注入到底层 runner / bridge 的 event sinks（trace 落盘等）。
            缺省空列表。
        instructions: 透传给 :meth:`SessionEngine.build`，构造默认 AgentSpec
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
        model_catalog_manager: 统一模型 catalog 门户。task 的 ``preset_id``
            与默认 selection 均通过该门户解析。
        resolved_model: 本次装配的默认 immutable 模型快照；缺省时由 Manager
            根据 ``config.model`` 解析。

    Returns:
        ``(runtime, bridge)`` 二元组：
        - ``runtime``: 调用方需保留引用，最终 ``await runtime.aclose()`` 释放
          底层 httpx 连接池。
        - ``bridge``: 已就绪的 :class:`ExecutionBridge`，只接受
          ``ScheduledRunManager`` 完成准入和 ID 分配后的执行请求。
    """
    sinks = list(event_sinks or [])
    catalog_manager = model_catalog_manager or ModelCatalogManager()
    default_model = resolved_model or catalog_manager.resolve_runtime(config.model)
    resolved_tool_context_metadata: dict[str, Any] = dict(tool_context_metadata or {})
    default_cwd = _metadata_cwd_string(resolved_tool_context_metadata)
    resolved_factory = session_factory or _default_cron_session_factory(
        config,
        session_bootstrap,
        model_name=default_model.name,
        default_cwd=default_cwd,
    )
    session_factory_for_agent = (
        None
        if session_factory is not None
        else _default_cron_session_factory_for_agent(
            config,
            session_bootstrap,
            default_cwd=default_cwd,
        )
    )
    runtime = SessionEngine.build(
        config,
        event_sinks=sinks,
        tools=tools,
        enabled_tool_names=enabled_tool_names,
        instructions=instructions,
        session_factory=resolved_factory,
        tool_context_metadata=resolved_tool_context_metadata,
        model_catalog_manager=catalog_manager,
        model_config=default_model,
    )
    bridge = ExecutionBridge(
        runtime=runtime,
        llm=runtime.llm,
        tools=runtime.tools,
        enabled_tool_names=runtime.enabled_tool_names,
        inner_approval=runtime.approval,
        session_factory=runtime.session_factory,
        session_factory_for_agent=session_factory_for_agent,
        event_sinks=sinks,
        agent_spec=runtime.agent_spec,
        store=store,
        dispatcher=dispatcher,
        interactive_approval_factory=interactive_approval_factory,
        tool_context_metadata=resolved_tool_context_metadata,
        tool_context_metadata_factory=tool_context_metadata_factory,
        model_catalog_manager=catalog_manager,
        default_model=default_model,
        base_config=config,
        trace_dir=trace_dir,
    )
    return runtime, bridge


def build_scheduled_run_manager(
    config: Config,
    store: Store,
    *,
    dispatcher_factory_builder: Callable[
        [SessionEngine],
        ScheduledRunDispatcherFactory,
    ],
    event_sinks: list[EventSink] | None = None,
    tools: ToolLookup | Mapping[str, Tool] | None = None,
    enabled_tool_names: list[str] | None = None,
    instructions: str | None = None,
    session_factory: Callable[[str], Session] | None = None,
    session_bootstrap: SessionBootstrap | None = None,
    dispatcher: DeliveryDispatcher | None = None,
    lifecycle_sink: RunLifecycleSink | None = None,
    interactive_approval_factory: (
        Callable[[ScheduledTask], ApprovalProvider | None] | None
    ) = None,
    tool_context_metadata: Mapping[str, Any] | None = None,
    tool_context_metadata_factory: (
        Callable[[ScheduledTask], Mapping[str, Any] | None] | None
    ) = None,
    model_catalog_manager: ModelCatalogManager | None = None,
    resolved_model: ResolvedModelConfig | None = None,
    trace_dir: Path | None = None,
) -> tuple[SessionEngine, ScheduledRunManager]:
    """装配生产 ticker 使用的 SessionEngine + ScheduledRunManager。"""
    runtime, bridge = build_cron_execution_bridge(
        config,
        store,
        event_sinks=event_sinks,
        tools=tools,
        enabled_tool_names=enabled_tool_names,
        instructions=instructions,
        session_factory=session_factory,
        session_bootstrap=session_bootstrap,
        dispatcher=dispatcher,
        lifecycle_sink=lifecycle_sink,
        interactive_approval_factory=interactive_approval_factory,
        tool_context_metadata=tool_context_metadata,
        tool_context_metadata_factory=tool_context_metadata_factory,
        model_catalog_manager=model_catalog_manager,
        resolved_model=resolved_model,
        trace_dir=trace_dir,
    )
    manager = ScheduledRunManager(
        bridge=bridge,
        dispatcher_factory=dispatcher_factory_builder(runtime),
        max_inflight=config.scheduler.max_inflight,
        lifecycle_sink=lifecycle_sink,
    )
    return runtime, manager


__all__ = ["build_cron_execution_bridge", "build_scheduled_run_manager"]
