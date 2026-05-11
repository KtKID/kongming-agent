"""EvolutionManager — 频道无关的 evolution 子系统门面。

频道方（claude_code / codex / generic_chat）只通过本类的公开 API 接入：
- ``enabled``：总开关
- ``notify_user_message``：fire-and-forget 触发 cadence + reviewer spawn
- ``register_event_route`` / ``unregister_event_route``：事件路由管理
- ``aclose``：释放资源

所有内部装配（mini ToolRegistry / state_store / event_bus / reviewer spawn）
对外完全黑盒。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evolution.event_bus import EvolutionEventBus
from evolution.models import TranscriptWindow
from evolution.state_store import EvolutionStateStore
from evolution.store import EvolutionStore, resolve_evolution_root

if TYPE_CHECKING:
    from config_loader.models import Config
    from core.contracts import EventSink
    from evolution.transcript_provider import TranscriptProvider

__all__ = ["EvolutionManager"]

logger = logging.getLogger(__name__)


class EvolutionManager:
    """Evolution 子系统门面。频道方只通过本类的公开 API 接入。"""

    def __init__(
        self,
        *,
        config: Config,
        kongming_home: Path,
    ) -> None:
        self._config = config
        self._learning = config.evolution.learning
        self._kongming_home = kongming_home
        self._event_bus = EvolutionEventBus()
        self._bg_tasks: set[asyncio.Task[Any]] = set()

        # 内部装配 state_store / evolution_store / mini_registry
        root_dir = resolve_evolution_root(self._learning.root_path)
        self._state_store = EvolutionStateStore(root_dir)
        self._evolution_store = EvolutionStore(
            root_dir=root_dir,
            state_store=self._state_store,
            event_sinks=(self._event_bus,),
        )

        # mini ToolRegistry：只含 evolution_write
        from tools import ToolRegistry

        self._mini_registry = ToolRegistry()

        if self._learning.enabled:
            from tools.evolution_write_tool import build_evolution_write_tool

            tool = build_evolution_write_tool(
                self._evolution_store,
                min_confidence=self._learning.nutrient_confidence_threshold,
                max_nutrients=self._learning.max_nutrients,
                event_sinks=(self._event_bus,),
            )
            self._mini_registry.register(tool)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """总开关——绑定 ``config.evolution.learning.enabled``。

        Disabled 时所有公开 API 都是 no-op，频道方无需做开关判断。
        """
        return self._learning.enabled

    async def notify_user_message(
        self,
        *,
        thread_id: str,
        provider: TranscriptProvider,
        cwd: str,
    ) -> None:
        """频道方在用户消息入口调用——fire-and-forget。

        内部：cadence 判断 → provider.build_window → spawn reviewer → 落盘 + 发事件。
        异常吞 + log warning，绝不冒泡。
        """
        if not self.enabled:
            return
        try:
            await self._do_notify(thread_id=thread_id, provider=provider, cwd=cwd)
        except Exception as exc:
            logger.warning(
                "evolution_manager.notify_user_message failed for %s: %s",
                thread_id,
                exc,
            )

    def register_event_route(self, thread_id: str, sink: EventSink) -> None:
        """频道方在 ws 连接建立时调用。"""
        self._event_bus.register(thread_id, sink)

    def unregister_event_route(self, thread_id: str) -> None:
        """频道方在 ws 断开时调用。幂等。"""
        self._event_bus.unregister(thread_id)

    async def aclose(self) -> None:
        """释放资源。app shutdown 时调。"""
        # 等待所有后台 reviewer tasks 完成（最多 drain 秒）
        if self._bg_tasks:
            drain = self._learning.drain_on_close_seconds
            _, pending = await asyncio.wait(
                self._bg_tasks,
                timeout=drain,
            )
            for task in pending:
                task.cancel()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _do_notify(
        self,
        *,
        thread_id: str,
        provider: TranscriptProvider,
        cwd: str,
    ) -> None:
        """notify_user_message 的内部实现——不吞异常，由外层 catch。"""
        learning = self._learning

        # 1. cadence: 记录 + 判断
        current = await self._state_store.record_parent_run(
            session_id=thread_id,
            user_turn_count=0,  # claude 频道 user_turn_count ≈ run_count
        )

        if current.run_count < learning.min_user_turns:
            return
        if current.run_count % learning.every_n_runs != 0:
            return

        # 2. run_id
        # v0.1 hardcode channel_id；M1.5 改成 provider.channel_id 拼接
        run_id = f"run-{provider.channel_id}-{thread_id}-{current.run_count}"

        # 3. transcript
        window = await provider.build_window(
            run_id=run_id,
            max_messages=learning.max_history_messages,
        )

        if not window.messages:
            await self._state_store.mark_review_result(
                session_id=thread_id,
                run_id=run_id,
                status="skipped_empty_window",
            )
            return

        # 4. spawn reviewer (后台 task)
        task = asyncio.create_task(
            self._run_review(
                thread_id=thread_id,
                run_id=run_id,
                window=window,
            ),
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _run_review(
        self,
        *,
        thread_id: str,
        run_id: str,
        window: TranscriptWindow,
    ) -> None:
        """后台执行 child reviewer。"""
        from evolution.reviewer_runtime import run_child_review

        stub = self._build_stub_parent_runtime()
        try:
            await run_child_review(
                parent_runtime=stub,
                window=window,
                trigger_reason="cadence_evolution_manager",
                timeout_seconds=self._learning.review_timeout_seconds,
                max_nutrients=self._learning.max_nutrients,
                min_confidence=self._learning.nutrient_confidence_threshold,
            )
        except Exception as exc:
            logger.warning(
                "evolution_manager._run_review failed for %s: %s",
                run_id,
                exc,
            )
        finally:
            await stub.aclose()

    def _build_stub_parent_runtime(self) -> Any:
        """构造 ad-hoc NativeRuntime 当 reviewer 的 parent 容器。"""
        from core.agent_spec import AgentSpec
        from core.session import InMemorySession
        from evolution.reviewer_runtime import REVIEWER_TOOL_NAME
        from executors.agent_runtime.native_runtime import NativeRuntime
        from tools import AutoAllowApproval

        learning = self._learning

        # 3 级 model fallback
        model = learning.model_name or self._config.model.name
        if not model:
            raise ValueError("evolution learning has no model resolvable")

        stub_spec = AgentSpec(
            name="evolution-stub-parent",
            instructions="",
            default_model=model,
            tool_names=(REVIEWER_TOOL_NAME,),
            max_turns=1,
            metadata={"evolution_role": "stub_parent"},
            reasoning_effort=learning.reasoning_effort,
        )
        return NativeRuntime.build(
            self._config,
            event_sinks=[self._event_bus],
            approval=AutoAllowApproval(),
            tools=self._mini_registry,
            enabled_tool_names=[REVIEWER_TOOL_NAME],
            session_factory=lambda sid: InMemorySession(session_id=sid),
            agent_spec=stub_spec,
        )
