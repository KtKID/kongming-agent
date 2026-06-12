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
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evolution.event_bus import EvolutionEventBus
from evolution.models import TranscriptWindow
from evolution.state_store import EvolutionStateStore
from evolution.store import EvolutionStore, resolve_evolution_root

if TYPE_CHECKING:
    from core.contracts import EventSink
    from evolution.transcript_provider import TranscriptProvider
    from infrastructure.config.models import Config

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
        from evolution.logging_setup import setup_evolution_logging

        self._config = config
        self._learning = config.evolution.learning
        self._kongming_home = kongming_home

        # 独立日志：全量 DEBUG 写 .kongming/logs/evolution.log
        self._log_path = setup_evolution_logging(kongming_home)
        logger.info("EvolutionManager init: home=%s log=%s", kongming_home, self._log_path)

        self._event_bus = EvolutionEventBus()
        self._bg_tasks: set[asyncio.Task[Any]] = set()

        # 内部装配 state_store / evolution_store / mini_registry
        root_dir = resolve_evolution_root(
            self._learning.root_path,
            kongming_home=kongming_home,
        )
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
            from tools.builtin.evolution_write_tool import build_evolution_write_tool

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
        logger.debug("evolution notify_user_message called: thread=%s", thread_id)
        try:
            await self._do_notify(thread_id=thread_id, provider=provider, cwd=cwd)
        except Exception as exc:
            logger.error(
                "evolution notify FAILED: thread=%s error_type=%s error=%s",
                thread_id,
                type(exc).__name__,
                exc or "(empty)",
                exc_info=True,
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

        logger.info(
            "evolution cadence: thread=%s run_count=%d every_n=%d min=%d",
            thread_id,
            current.run_count,
            learning.every_n_runs,
            learning.min_user_turns,
        )

        if current.run_count < learning.min_user_turns:
            logger.info(
                "evolution skip: run_count=%d < min_user_turns=%d",
                current.run_count,
                learning.min_user_turns,
            )
            return
        if current.run_count % learning.every_n_runs != 0:
            logger.info(
                "evolution skip: run_count=%d %% every_n=%d != 0 (next at %d)",
                current.run_count,
                learning.every_n_runs,
                current.run_count
                + (learning.every_n_runs - current.run_count % learning.every_n_runs),
            )
            return

        # 2. run_id
        run_id = f"run-{provider.channel_id}-{thread_id}-{current.run_count}"
        logger.info("evolution triggered: run_id=%s", run_id)

        # 3. transcript
        window = await provider.build_window(
            run_id=run_id,
            max_messages=learning.max_history_messages,
        )

        if not window.messages:
            logger.info("evolution skip: empty transcript window for %s", run_id)
            await self._state_store.mark_review_result(
                session_id=thread_id,
                run_id=run_id,
                status="skipped_empty_window",
            )
            return

        logger.info(
            "evolution spawning reviewer: run_id=%s messages=%d",
            run_id,
            len(window.messages),
        )

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

        # --- 日志：子 agent 输入 ---
        first_msg = window.messages[0].content[:80] if window.messages else ""
        last_msg = window.messages[-1].content[:80] if window.messages else ""
        logger.info(
            "evolution reviewer INPUT: run_id=%s thread=%s messages=%d turns=%s "
            "first_msg=%r last_msg=%r timeout=%.1fs",
            run_id,
            thread_id,
            len(window.messages),
            window.included_turns,
            first_msg,
            last_msg,
            self._learning.review_timeout_seconds,
        )

        # --- emit: evolution.review.started ---

        from core.contracts import Event

        review_id = f"evo-review:{run_id}"
        await self._event_bus.emit(
            Event(
                kind="evolution.review.started",
                run_id=run_id,
                payload={
                    "review_id": review_id,
                    "session_id": thread_id,
                    "timeout_seconds": self._learning.review_timeout_seconds,
                },
            )
        )

        stub = self._build_stub_parent_runtime()
        try:
            outcome = await run_child_review(
                parent_runtime=stub,
                window=window,
                trigger_reason="cadence_evolution_manager",
                timeout_seconds=self._learning.review_timeout_seconds,
                max_nutrients=self._learning.max_nutrients,
                min_confidence=self._learning.nutrient_confidence_threshold,
            )

            # --- 日志：子 agent 输出（解析 ChildReviewOutcome）---
            logger.info(
                "evolution reviewer OUTPUT: run_id=%s write_ok=%s write_status=%s "
                "timed_out=%s duration_ms=%d",
                run_id,
                outcome.write_ok,
                outcome.write_status,
                outcome.timed_out,
                outcome.duration_ms,
            )

            # 落盘结果
            if outcome.write_ok and outcome.write_data:
                nutrient_ids = outcome.write_data.get("written_nutrient_ids", [])
                review_path = outcome.write_data.get("review_path", "")
                logger.info(
                    "evolution reviewer STORED: run_id=%s review_path=%s nutrients=%d ids=%s",
                    run_id,
                    review_path,
                    len(nutrient_ids) if isinstance(nutrient_ids, (list, tuple)) else 0,
                    nutrient_ids,
                )
            elif outcome.write_ok:
                logger.info(
                    "evolution reviewer STORED: run_id=%s (write_ok but no write_data)", run_id
                )
            else:
                logger.error(
                    "evolution reviewer WRITE FAILED: run_id=%s write_status=%s write_error=%s",
                    run_id,
                    outcome.write_status,
                    outcome.write_error,
                )

            if outcome.timed_out:
                logger.warning(
                    "evolution reviewer TIMEOUT: run_id=%s duration_ms=%d timeout=%.1fs "
                    "write_ok_before_timeout=%s",
                    run_id,
                    outcome.duration_ms,
                    outcome.timeout_seconds or 0,
                    outcome.write_ok,
                )

            # --- emit: evolution.review.completed / failed ---
            if outcome.write_ok:
                await self._event_bus.emit(
                    Event(
                        kind="evolution.review.completed",
                        run_id=run_id,
                        payload={
                            "review_id": review_id,
                            "session_id": thread_id,
                            "write_status": outcome.write_status,
                            "duration_ms": outcome.duration_ms,
                            "timeout_hit": outcome.timed_out,
                            "timeout_seconds": outcome.timeout_seconds,
                            "nutrients_written": outcome.write_data.get("nutrients_written")
                            if isinstance(outcome.write_data, dict)
                            else None,
                            "written_nutrient_ids": outcome.write_data.get("written_nutrient_ids")
                            if isinstance(outcome.write_data, dict)
                            else None,
                            "review_summary": outcome.write_data.get("review_summary")
                            if isinstance(outcome.write_data, dict)
                            else None,
                            "nutrient_summaries": outcome.write_data.get("nutrient_summaries")
                            if isinstance(outcome.write_data, dict)
                            else None,
                        },
                    )
                )
            else:
                await self._event_bus.emit(
                    Event(
                        kind="evolution.review.failed",
                        run_id=run_id,
                        payload={
                            "review_id": review_id,
                            "session_id": thread_id,
                            "write_status": outcome.write_status,
                            "duration_ms": outcome.duration_ms,
                            "timeout_hit": outcome.timed_out,
                            "error": outcome.write_error or "",
                        },
                    )
                )

        except Exception as exc:
            logger.error(
                "evolution reviewer EXCEPTION: run_id=%s error_type=%s error=%s",
                run_id,
                type(exc).__name__,
                exc or "(empty — likely TimeoutError)",
                exc_info=True,
            )
            # emit failed on exception path
            with contextlib.suppress(Exception):
                await self._event_bus.emit(
                    Event(
                        kind="evolution.review.failed",
                        run_id=run_id,
                        payload={
                            "review_id": review_id,
                            "session_id": thread_id,
                            "error_kind": type(exc).__name__,
                            "error": str(exc) or "(empty)",
                        },
                    )
                )
        finally:
            await stub.aclose()

    def _build_stub_parent_runtime(self) -> Any:
        """构造 ad-hoc NativeRuntime 当 reviewer 的 parent 容器。"""
        import os

        from core.agent_spec import AgentSpec
        from core.session import InMemorySession
        from evolution.reviewer_runtime import REVIEWER_TOOL_NAME
        from runtime_assembly.native_runtime import NativeRuntime
        from tools import AutoAllowApproval

        learning = self._learning

        # 3 级 model fallback
        model = learning.model_name or self._config.model.name
        if not model:
            raise ValueError("evolution learning has no model resolvable")

        # reviewer 独立模型配置覆盖（跟 web preset 同模式）
        model_overrides: dict[str, Any] = {"name": model}
        if learning.base_url:
            model_overrides["base_url"] = learning.base_url
        if learning.api_key_env:
            model_overrides["api_key"] = os.environ.get(learning.api_key_env, "")
        if learning.provider:
            model_overrides["provider"] = learning.provider
        if learning.reasoning_effort:
            model_overrides["reasoning_effort"] = learning.reasoning_effort
        preset_model = self._config.model.model_copy(update=model_overrides)
        preset_cfg = self._config.model_copy(update={"model": preset_model})

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
            preset_cfg,
            event_sinks=[self._event_bus],
            approval=AutoAllowApproval(),
            tools=self._mini_registry,
            enabled_tool_names=[REVIEWER_TOOL_NAME],
            session_factory=lambda sid: InMemorySession(session_id=sid),
            agent_spec=stub_spec,
        )
