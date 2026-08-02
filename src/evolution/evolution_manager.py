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
import secrets
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.agent_spec import coerce_reasoning_effort
from core.clock import now_epoch_ms
from core.contracts import Event, Tool
from evolution.event_bus import EvolutionEventBus
from evolution.events import (
    EvolutionReviewCompletedPayload,
    EvolutionReviewDrainTimeoutPayload,
    EvolutionReviewFailedPayload,
    EvolutionReviewStartedPayload,
)
from evolution.models import (
    MAX_MANUAL_REVIEW_FOCUS_CHARS,
    ApplyJob,
    DecisionItem,
    DecisionRecord,
    DecisionSummary,
    DecisionTarget,
    DecisionValue,
    EvolutionNutrient,
    EvolutionReviewPlan,
    EvolutionReviewTrigger,
    ManualReviewQueueStatus,
    ManualReviewRequest,
    ReviewNoticeSnapshot,
    ReviewResult,
    TranscriptWindow,
)
from evolution.state_store import EvolutionStateStore
from evolution.store import EvolutionStore, resolve_evolution_root
from evolution.trigger_diagnostics import log_trigger_block

if TYPE_CHECKING:
    from core.contracts import EventSink, Session
    from core.result import Result
    from evolution.transcript_provider import TranscriptProvider
    from infrastructure.config.models import Config
    from tools import ToolRegistry

__all__ = ["EvolutionManager"]

logger = logging.getLogger(__name__)

_PRIVATE_TOOL_NAMES = frozenset({"evolution_write"})
_LIFECYCLE_BOUND_TOOL_NAMES = frozenset({"request_evolution_review"})


def _now_ms() -> int:
    return now_epoch_ms()


def _review_id_for_run(run_id: str) -> str:
    return f"evo-review:{run_id}"


def _decision_target_for_value(value: str) -> DecisionTarget | None:
    if value == "accept_memory":
        return "memory"
    if value == "accept_skill":
        return "skill"
    return None


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
        self._bg_tasks: dict[asyncio.Task[Any], tuple[str, tuple[EventSink, ...]]] = {}
        self._apply_queue_lock = asyncio.Lock()
        self._manual_review_lock = asyncio.Lock()
        self._manual_review_requests: dict[tuple[str, str], ManualReviewRequest] = {}

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

    @property
    def private_tool_names(self) -> frozenset[str]:
        """返回主 Agent 工具面必须隐藏的 reviewer 私有 Tool 名。"""
        return self.runtime_private_tool_names()

    @property
    def lifecycle_bound_tool_names(self) -> frozenset[str]:
        """返回只允许安装了 evolution lifecycle 的 runtime 启用的 Tool 名。"""
        return self.runtime_lifecycle_bound_tool_names()

    @classmethod
    def runtime_private_tool_names(cls) -> frozenset[str]:
        """返回宿主装配期需要保留的 reviewer 私有 Tool 名合同。"""
        return _PRIVATE_TOOL_NAMES

    @classmethod
    def runtime_lifecycle_bound_tool_names(cls) -> frozenset[str]:
        """返回宿主装配期需要 lifecycle 消费者的公开 Tool 名合同。"""
        return _LIFECYCLE_BOUND_TOOL_NAMES

    @classmethod
    def filter_runtime_tool_names(
        cls,
        tool_names: Sequence[str],
        *,
        lifecycle_bound: bool,
    ) -> list[str]:
        """按 lifecycle 安装状态过滤 evolution 的公开/私有工具边界。"""
        excluded = set(cls.runtime_private_tool_names())
        if not lifecycle_bound:
            excluded.update(cls.runtime_lifecycle_bound_tool_names())
        return [name for name in tool_names if name not in excluded]

    async def queue_manual_review(
        self,
        *,
        session_id: str,
        run_id: str,
        focus: str | None,
    ) -> ManualReviewQueueStatus:
        """为当前主 run 登记一次幂等的 after-run 审查请求。"""
        if not self.enabled:
            raise RuntimeError("evolution learning is disabled")
        normalized_session_id = session_id.strip()
        normalized_run_id = run_id.strip()
        if not normalized_session_id:
            raise ValueError("session_id must be a non-empty string")
        if not normalized_run_id:
            raise ValueError("run_id must be a non-empty string")
        normalized_focus = focus.strip() if isinstance(focus, str) else None
        if normalized_focus is not None and len(normalized_focus) > MAX_MANUAL_REVIEW_FOCUS_CHARS:
            raise ValueError(f"focus must be at most {MAX_MANUAL_REVIEW_FOCUS_CHARS} characters")
        request = ManualReviewRequest(
            session_id=normalized_session_id,
            run_id=normalized_run_id,
            focus=normalized_focus or None,
            requested_at_ms=_now_ms(),
        )
        key = (request.session_id, request.run_id)
        async with self._manual_review_lock:
            if key in self._manual_review_requests:
                return ManualReviewQueueStatus.ALREADY_QUEUED
            self._manual_review_requests[key] = request
        logger.info(
            "evolution manual review queued: thread=%s run=%s focus=%r",
            request.session_id,
            request.run_id,
            request.focus,
        )
        return ManualReviewQueueStatus.QUEUED

    async def start_manual_command_review(
        self,
        *,
        parent_runtime: Any,
        session: Session,
        thread_id: str,
    ) -> str:
        """立即使用当前线程历史启动一轮独立 child reviewer。

        该入口服务宿主控制命令。输入为当前线程 runtime、对应 Session 与 thread id；
        输出为本次 review 的独立 run id。命令本身不进入主 Agent run，也不修改
        session history。
        """
        if not self.enabled:
            raise RuntimeError("evolution learning is disabled")
        normalized_thread_id = thread_id.strip()
        if not normalized_thread_id:
            raise ValueError("thread_id must be a non-empty string")
        if parent_runtime.agent_spec.metadata.get("evolution_role") == "reviewer":
            raise RuntimeError("reviewer runtime cannot trigger another evolution review")

        from evolution.evidence_selector import build_transcript_window, count_user_turns
        from evolution.reviewer_runtime import REVIEWER_TOOL_NAME

        if REVIEWER_TOOL_NAME not in parent_runtime.tools:
            raise RuntimeError("parent runtime tools missing evolution_write")

        history = await session.history()
        final_message = history[-1] if history and history[-1].role == "assistant" else None
        run_id = f"run-manual-command-{normalized_thread_id}-{_now_ms()}-{secrets.token_hex(4)}"
        window = build_transcript_window(
            session_id=normalized_thread_id,
            run_id=run_id,
            history=history,
            final_message=final_message,
            max_messages=self._learning.max_history_messages,
        )
        if not window.messages:
            raise ValueError("current thread has no conversation history")

        user_turn_count = count_user_turns(history)
        self._start_review_task(
            thread_id=normalized_thread_id,
            run_id=run_id,
            window=window,
            parent_runtime=parent_runtime,
            trigger_reason=EvolutionReviewTrigger.MANUAL_COMMAND,
            focus=None,
            extra_sinks=(),
            started_user_turn_count=user_turn_count,
            started_included_turns=window.included_turns,
        )
        logger.info(
            "evolution manual command review started: thread=%s run=%s messages=%d",
            normalized_thread_id,
            run_id,
            len(window.messages),
        )
        return run_id

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
            log_trigger_block(
                "notify_exception",
                thread_id=thread_id,
                detail=f"notify_user_message {type(exc).__name__}: {exc or '(empty)'}",
                exc_info=True,
            )

    async def notify_runtime_run(
        self,
        parent_runtime: Any,
        session: Session,
        result: Result,
    ) -> None:
        """Lifecycle after_run 入口：runtime 只上报结果，cadence 由本 manager 推进。"""
        if not self.enabled:
            return
        manual_request = await self._consume_manual_review(
            session_id=result.session_id,
            run_id=result.run_id,
        )
        if result.status != "completed":
            log_trigger_block(
                "run_not_completed",
                thread_id=result.session_id,
                run_id=result.run_id,
                detail=(
                    f"status={result.status} manual_request_consumed={manual_request is not None}"
                ),
            )
            return
        if parent_runtime.agent_spec.metadata.get("evolution_role") == "reviewer":
            log_trigger_block(
                "reviewer_self_loop",
                thread_id=result.session_id,
                run_id=result.run_id,
            )
            return

        from evolution.reviewer_runtime import REVIEWER_TOOL_NAME

        if REVIEWER_TOOL_NAME not in parent_runtime.tools:
            log_trigger_block(
                "missing_evolution_write_tool",
                thread_id=result.session_id,
                run_id=result.run_id,
                detail=(
                    "parent runtime tools missing evolution_write; "
                    "check EvolutionManager.register_runtime_tools in host assembly"
                ),
            )
            return
        try:
            await self._do_notify_runtime_run(
                parent_runtime=parent_runtime,
                session=session,
                result=result,
                manual_request=manual_request,
            )
        except Exception as exc:
            log_trigger_block(
                "notify_exception",
                thread_id=result.session_id,
                run_id=result.run_id,
                detail=f"notify_runtime_run {type(exc).__name__}: {exc or '(empty)'}",
                exc_info=True,
            )

    def register_event_route(self, thread_id: str, sink: EventSink) -> None:
        """频道方在 ws 连接建立时调用。"""
        self._event_bus.register(thread_id, sink)

    def unregister_event_route(
        self,
        thread_id: str,
        sink: EventSink | None = None,
    ) -> None:
        """频道方在 ws 断开时调用；传 sink 可安全释放单个连接登记。"""
        self._event_bus.unregister(thread_id, sink)

    def register_runtime_tools(
        self,
        registry: ToolRegistry,
        *,
        event_sinks: Sequence[EventSink] = (),
    ) -> bool:
        """向主 runtime 注册公开请求 Tool 与 reviewer 私有写入 Tool。"""
        if not self.enabled:
            return False
        from evolution.review_request_tool import build_request_evolution_review_tool
        from tools.builtin.evolution_write_tool import build_evolution_write_tool

        registry.register(cast(Tool, build_request_evolution_review_tool(self)))
        registry.register(
            cast(
                Tool,
                build_evolution_write_tool(
                    self._evolution_store,
                    min_confidence=self._learning.nutrient_confidence_threshold,
                    max_nutrients=self._learning.max_nutrients,
                    event_sinks=event_sinks,
                ),
            )
        )
        return True

    def enabled_tool_names(
        self,
        tool_names: Sequence[str],
        *,
        lifecycle_bound: bool,
    ) -> list[str]:
        """按 runtime 是否安装 evolution lifecycle 过滤公开与私有工具面。"""
        return self.filter_runtime_tool_names(
            tool_names,
            lifecycle_bound=lifecycle_bound,
        )

    async def list_review_records_for_session(
        self,
        session_id: str,
    ) -> tuple[tuple[ReviewResult, DecisionRecord | None], ...]:
        reviews = await self._evolution_store.list_reviews_for_session(session_id)
        out: list[tuple[ReviewResult, DecisionRecord | None]] = []
        for review in reviews:
            decision = await self._evolution_store.read_decision(_review_id_for_run(review.run_id))
            out.append((review, decision))
        return tuple(out)

    async def list_notice_snapshots_for_session(
        self,
        session_id: str,
    ) -> tuple[ReviewNoticeSnapshot, ...]:
        return await self._evolution_store.list_notice_snapshots_for_session(session_id)

    async def apply_review_decision(
        self,
        *,
        thread_id: str,
        review_id: str,
        nutrient_id: str,
        decision: DecisionValue,
        workspace_root: Path,
    ) -> tuple[ReviewResult, DecisionRecord]:
        async with self._apply_queue_lock:
            review = await self._require_review(thread_id=thread_id, review_id=review_id)
            nutrient = self._require_nutrient(review=review, nutrient_id=nutrient_id)
            existing = await self._evolution_store.read_decision(review_id)
            items = list(existing.items if existing is not None else ())
            now_ms = _now_ms()
            new_item = DecisionItem(
                nutrient_id=nutrient_id,
                decision=decision,
                target=_decision_target_for_value(decision),
                decided_at_ms=now_ms,
            )
            replaced = False
            for index, item in enumerate(items):
                if item.nutrient_id == nutrient_id:
                    items[index] = new_item
                    replaced = True
                    break
            if not replaced:
                items.append(new_item)
            record = await self._evolution_store.write_decision(
                DecisionRecord(
                    review_id=review_id,
                    session_id=thread_id,
                    run_id=review.run_id,
                    summary=DecisionSummary(
                        total=len(review.nutrients),
                        accepted_memory=0,
                        accepted_skill=0,
                        ignored=0,
                        pending=len(review.nutrients),
                    ),
                    items=tuple(items),
                )
            )
            decision_item = next(
                (item for item in record.items if item.nutrient_id == nutrient_id), None
            )
            if decision_item is None:
                raise ValueError(f"decision item missing: {nutrient_id}")
            record = await self._apply_decision_item(
                review_id=review_id,
                session_id=thread_id,
                run_id=review.run_id,
                nutrient=nutrient,
                decision=decision_item,
                workspace_root=workspace_root,
            )
            return review, record

    async def reapply_review_decisions(
        self,
        *,
        thread_id: str,
        review_id: str,
        workspace_root: Path,
    ) -> tuple[ReviewResult, DecisionRecord]:
        async with self._apply_queue_lock:
            review = await self._require_review(thread_id=thread_id, review_id=review_id)
            record = await self._evolution_store.read_decision(review_id)
            if record is None:
                raise KeyError(f"decision record not found: {review_id}")
            actionable = [
                item
                for item in record.items
                if item.decision in {"accept_memory", "accept_skill"}
                and item.applied_status in {"pending", "failed"}
            ]
            nutrient_map = {nutrient.nutrient_id: nutrient for nutrient in review.nutrients}
            for item in actionable:
                nutrient = nutrient_map.get(item.nutrient_id)
                if nutrient is None:
                    continue
                record = await self._apply_decision_item(
                    review_id=review_id,
                    session_id=thread_id,
                    run_id=review.run_id,
                    nutrient=nutrient,
                    decision=item,
                    workspace_root=workspace_root,
                )
            return review, record

    async def recover_pending_apply_jobs(self) -> tuple[ApplyJob, ...]:
        async with self._apply_queue_lock:
            from evolution.apply_executor import recover_pending_apply_jobs

            return await recover_pending_apply_jobs(
                self._config,
                store=self._evolution_store,
                kongming_home=self._kongming_home,
            )

    async def aclose(self) -> None:
        """释放资源。app shutdown 时调。"""
        # 等待所有后台 reviewer tasks 完成（最多 drain 秒）
        if self._bg_tasks:
            drain = self._learning.drain_on_close_seconds
            pending_items = list(self._bg_tasks.items())
            _, pending = await asyncio.wait(
                [task for task, _ in pending_items],
                timeout=drain,
            )
            if pending:
                review_ids = [self._bg_tasks[task][0] for task in pending if task in self._bg_tasks]
                extra_sinks: list[EventSink] = []
                for task in pending:
                    if task in self._bg_tasks:
                        extra_sinks.extend(self._bg_tasks[task][1])
                await self._emit_event(
                    Event(
                        kind="evolution.review.drain_timeout",
                        run_id="runtime-close",
                        payload=EvolutionReviewDrainTimeoutPayload.from_review_ids(
                            pending_review_ids=review_ids,
                            timeout_seconds=drain,
                        ).to_payload(),
                    ),
                    extra_sinks=tuple(extra_sinks),
                )
                for task in pending:
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _consume_manual_review(
        self,
        *,
        session_id: str,
        run_id: str,
    ) -> ManualReviewRequest | None:
        key = (session_id, run_id)
        async with self._manual_review_lock:
            return self._manual_review_requests.pop(key, None)

    async def _require_review(self, *, thread_id: str, review_id: str) -> ReviewResult:
        reviews = await self._evolution_store.list_reviews_for_session(thread_id)
        review = next(
            (item for item in reviews if _review_id_for_run(item.run_id) == review_id),
            None,
        )
        if review is None:
            raise KeyError(f"review not found: {review_id}")
        return review

    @staticmethod
    def _require_nutrient(*, review: ReviewResult, nutrient_id: str) -> EvolutionNutrient:
        nutrient = next(
            (item for item in review.nutrients if item.nutrient_id == nutrient_id),
            None,
        )
        if nutrient is None:
            raise KeyError(f"nutrient not found in review: {nutrient_id}")
        return nutrient

    async def _apply_decision_item(
        self,
        *,
        review_id: str,
        session_id: str,
        run_id: str,
        nutrient: EvolutionNutrient,
        decision: DecisionItem,
        workspace_root: Path,
    ) -> DecisionRecord:
        from evolution.apply_executor import build_apply_job, execute_apply_job

        job = build_apply_job(
            review_id=review_id,
            session_id=session_id,
            run_id=run_id,
            nutrient_id=decision.nutrient_id,
            decision=decision,
            workspace_root=workspace_root,
            created_at_ms=_now_ms(),
        )
        await self._evolution_store.write_apply_job(job)
        outcome = await execute_apply_job(
            cfg=self._config,
            store=self._evolution_store,
            job=job,
            nutrient=nutrient,
            kongming_home=self._kongming_home,
        )
        return outcome.decision_record

    async def _do_notify(
        self,
        *,
        thread_id: str,
        provider: TranscriptProvider,
        cwd: str,
    ) -> None:
        """notify_user_message 的内部实现——不吞异常，由外层 catch。"""
        learning = self._learning
        if not learning.auto_trigger_enabled:
            log_trigger_block(
                "automatic_trigger_disabled",
                thread_id=thread_id,
                detail="evolution.learning.auto_trigger_enabled=false",
            )
            return

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
            log_trigger_block(
                "below_min_user_turns",
                thread_id=thread_id,
                detail=(
                    f"run_count={current.run_count} < min_user_turns={learning.min_user_turns}"
                ),
            )
            return
        if current.run_count % learning.every_n_runs != 0:
            next_at = current.run_count + (
                learning.every_n_runs - current.run_count % learning.every_n_runs
            )
            log_trigger_block(
                "cadence_not_due",
                thread_id=thread_id,
                detail=(
                    f"run_count={current.run_count} % every_n={learning.every_n_runs} != 0 "
                    f"(next at {next_at})"
                ),
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
            log_trigger_block(
                "empty_transcript_window",
                thread_id=thread_id,
                run_id=run_id,
                detail="cadence hit but transcript window is empty",
            )
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
        self._start_review_task(
            thread_id=thread_id,
            run_id=run_id,
            window=window,
            parent_runtime=None,
            trigger_reason=EvolutionReviewTrigger.CADENCE_EVOLUTION_MANAGER,
            focus=None,
            extra_sinks=(),
        )

    async def _do_notify_runtime_run(
        self,
        *,
        parent_runtime: Any,
        session: Session,
        result: Result,
        manual_request: ManualReviewRequest | None,
    ) -> None:
        from evolution.evidence_selector import build_transcript_window, count_user_turns

        learning = self._learning
        history = await session.history()
        user_turn_count = count_user_turns(history)

        # run_count 真源 = session manifest（Session.get_run_count），不再自维护副本。
        # 仅用 update_session_meta 刷新 user_turn_count 旁路字段，不递增 run_count。
        run_count = await session.get_run_count()
        await self._state_store.update_session_meta(
            session_id=result.session_id,
            user_turn_count=user_turn_count,
        )
        plan = self._resolve_runtime_review_plan(
            result=result,
            manual_request=manual_request,
            user_turn_count=user_turn_count,
            run_count=run_count,
        )
        if plan is None:
            return

        window = build_transcript_window(
            session_id=result.session_id,
            run_id=result.run_id,
            history=history,
            final_message=result.final_message,
            max_messages=learning.max_history_messages,
        )
        if not window.messages:
            log_trigger_block(
                "empty_transcript_window",
                thread_id=result.session_id,
                run_id=result.run_id,
                detail=f"trigger={plan.trigger.value} but transcript window is empty",
            )
            await self._state_store.mark_review_result(
                session_id=result.session_id,
                run_id=result.run_id,
                status="skipped_empty_window",
            )
            return

        extra_sinks = tuple(parent_runtime.event_sinks)
        self._start_review_task(
            thread_id=result.session_id,
            run_id=result.run_id,
            window=window,
            parent_runtime=parent_runtime,
            trigger_reason=plan.trigger,
            focus=plan.focus,
            extra_sinks=extra_sinks,
            started_user_turn_count=user_turn_count,
            started_included_turns=window.included_turns,
        )

    def _resolve_runtime_review_plan(
        self,
        *,
        result: Result,
        manual_request: ManualReviewRequest | None,
        user_turn_count: int,
        run_count: int,
    ) -> EvolutionReviewPlan | None:
        learning = self._learning
        if manual_request is not None:
            logger.info(
                "evolution manual review selected: thread=%s run=%s focus=%r",
                result.session_id,
                result.run_id,
                manual_request.focus,
            )
            return EvolutionReviewPlan(
                trigger=EvolutionReviewTrigger.MANUAL_TOOL,
                focus=manual_request.focus,
            )
        if not learning.auto_trigger_enabled:
            log_trigger_block(
                "automatic_trigger_disabled",
                thread_id=result.session_id,
                run_id=result.run_id,
                detail="evolution.learning.auto_trigger_enabled=false",
            )
            return None
        if user_turn_count < learning.min_user_turns:
            log_trigger_block(
                "below_min_user_turns",
                thread_id=result.session_id,
                run_id=result.run_id,
                detail=(
                    f"user_turn_count={user_turn_count} < min_user_turns={learning.min_user_turns}"
                ),
            )
            return None
        logger.info(
            "evolution cadence: thread=%s run_count=%d every_n=%d min=%d",
            result.session_id,
            run_count,
            learning.every_n_runs,
            learning.min_user_turns,
        )
        if run_count % learning.every_n_runs != 0:
            next_at = run_count + (learning.every_n_runs - run_count % learning.every_n_runs)
            log_trigger_block(
                "cadence_not_due",
                thread_id=result.session_id,
                run_id=result.run_id,
                detail=(
                    f"run_count={run_count} % every_n={learning.every_n_runs} != 0 "
                    f"(next at {next_at})"
                ),
            )
            return None
        return EvolutionReviewPlan(trigger=EvolutionReviewTrigger.CADENCE)

    def _start_review_task(
        self,
        *,
        thread_id: str,
        run_id: str,
        window: TranscriptWindow,
        parent_runtime: Any | None,
        trigger_reason: EvolutionReviewTrigger,
        focus: str | None,
        extra_sinks: Sequence[EventSink],
        started_user_turn_count: int | None = None,
        started_included_turns: Sequence[int] | None = None,
    ) -> None:
        review_id = f"evo-review:{run_id}"
        task = asyncio.create_task(
            self._run_review(
                thread_id=thread_id,
                run_id=run_id,
                review_id=review_id,
                window=window,
                parent_runtime=parent_runtime,
                trigger_reason=trigger_reason,
                focus=focus,
                extra_sinks=tuple(extra_sinks),
                started_user_turn_count=started_user_turn_count,
                started_included_turns=started_included_turns,
            ),
            name=review_id,
        )
        self._bg_tasks[task] = (review_id, tuple(extra_sinks))

        def _discard_done(done: asyncio.Task[Any]) -> None:
            self._bg_tasks.pop(done, None)

        task.add_done_callback(_discard_done)

    async def _run_review(
        self,
        *,
        thread_id: str,
        run_id: str,
        review_id: str,
        window: TranscriptWindow,
        parent_runtime: Any | None,
        trigger_reason: EvolutionReviewTrigger,
        focus: str | None,
        extra_sinks: Sequence[EventSink],
        started_user_turn_count: int | None,
        started_included_turns: Sequence[int] | None,
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

        await self._emit_event(
            Event(
                kind="evolution.review.started",
                run_id=run_id,
                payload=EvolutionReviewStartedPayload(
                    review_id=review_id,
                    session_id=thread_id,
                    timeout_seconds=self._learning.review_timeout_seconds,
                    user_turn_count=started_user_turn_count,
                    included_turns=tuple(started_included_turns)
                    if started_included_turns is not None
                    else None,
                ).to_payload(),
            ),
            extra_sinks=extra_sinks,
        )

        review_runtime = parent_runtime or self._build_stub_parent_runtime()
        should_close_runtime = parent_runtime is None
        try:
            outcome = await run_child_review(
                parent_runtime=review_runtime,
                window=window,
                trigger_reason=trigger_reason.value,
                focus=focus,
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
                log_trigger_block(
                    "reviewer_write_failed",
                    thread_id=thread_id,
                    run_id=run_id,
                    detail=(
                        f"write_status={outcome.write_status} write_error={outcome.write_error}"
                    ),
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

            # reviewer 的 tool.call.start/end 只走 evolution event_bus，不转发到
            # 父 thread 的 extra_sinks（含 ThreadStatusEventSink）。这些事件的
            # run_id 属于 reviewer 子 agent，转发到父 thread 会把 phase 踩踏成
            # tool_calling，覆盖主 run 已发出的 complete 终态，导致前端停止按钮卡红。
            # 前端弹窗通知依赖 evolution.review.completed/failed（仍在下面走 extra_sinks）。
            visible_events = getattr(outcome, "visible_events", ())
            if isinstance(visible_events, (list, tuple)):
                for event in visible_events:
                    await self._emit_event(event, extra_sinks=())

            review_run_id, child_status, child_error = self._child_result_parts(outcome)

            # --- emit: evolution.review.completed / failed ---
            if outcome.write_ok:
                await self._mark_review_result_from_outcome(
                    session_id=thread_id,
                    run_id=run_id,
                    outcome=outcome,
                )
                await self._emit_event(
                    Event(
                        kind="evolution.review.completed",
                        run_id=run_id,
                        payload=EvolutionReviewCompletedPayload.from_child_outcome(
                            review_id=review_id,
                            review_run_id=review_run_id,
                            session_id=thread_id,
                            outcome=outcome,
                        ).to_payload(),
                    ),
                    extra_sinks=extra_sinks,
                )
                return
            await self._state_store.mark_review_result(
                session_id=thread_id,
                run_id=run_id,
                status="failed",
            )
            if child_status != "completed":
                child_error_kind = (
                    type(child_error).__name__ if child_error is not None else "ChildReviewerError"
                )
                child_error_message = (
                    child_error.message
                    if child_error is not None and hasattr(child_error, "message")
                    else f"child reviewer finished with status={child_status}"
                )
                await self._emit_event(
                    Event(
                        kind="evolution.review.failed",
                        run_id=run_id,
                        payload=EvolutionReviewFailedPayload(
                            review_id=review_id,
                            review_run_id=review_run_id,
                            session_id=thread_id,
                            error_kind=child_error_kind,
                            message=child_error_message,
                            child_status=child_status,
                            duration_ms=outcome.duration_ms,
                            timeout_hit=outcome.timed_out,
                            timeout_seconds=outcome.timeout_seconds,
                        ).to_payload(),
                    ),
                    extra_sinks=extra_sinks,
                )
            else:
                write_error_message = outcome.write_error or (
                    f"evolution_write did not succeed status={outcome.write_status}"
                )
                await self._emit_event(
                    Event(
                        kind="evolution.review.failed",
                        run_id=run_id,
                        payload=EvolutionReviewFailedPayload(
                            review_id=review_id,
                            review_run_id=review_run_id,
                            session_id=thread_id,
                            error_kind="EvolutionWriteError",
                            message=write_error_message,
                            write_status=outcome.write_status,
                            duration_ms=outcome.duration_ms,
                            timeout_hit=outcome.timed_out,
                            timeout_seconds=outcome.timeout_seconds,
                            error=outcome.write_error or "",
                        ).to_payload(),
                    ),
                    extra_sinks=extra_sinks,
                )

        except asyncio.CancelledError:
            await self._state_store.mark_review_result(
                session_id=thread_id,
                run_id=run_id,
                status="cancelled",
            )
            await self._emit_event(
                Event(
                    kind="evolution.review.failed",
                    run_id=run_id,
                    payload=EvolutionReviewFailedPayload(
                        review_id=review_id,
                        session_id=thread_id,
                        error_kind="cancelled",
                        timeout_hit=False,
                    ).to_payload(),
                ),
                extra_sinks=extra_sinks,
            )
            raise
        except Exception as exc:
            # TimeoutError = reviewer 超时且超时前未完成写入(已写入的超时在
            # run_child_review 内部消化为正常 outcome,不会走到这里)。
            timeout_hit = isinstance(exc, TimeoutError)
            log_trigger_block(
                "reviewer_timeout_no_write" if timeout_hit else "reviewer_exception",
                thread_id=thread_id,
                run_id=run_id,
                detail=f"{type(exc).__name__}: {exc or '(empty)'}",
                exc_info=not timeout_hit,
            )
            await self._state_store.mark_review_result(
                session_id=thread_id,
                run_id=run_id,
                status="failed",
            )
            # emit failed on exception path
            with contextlib.suppress(Exception):
                await self._emit_event(
                    Event(
                        kind="evolution.review.failed",
                        run_id=run_id,
                        payload=EvolutionReviewFailedPayload(
                            review_id=review_id,
                            session_id=thread_id,
                            error_kind=type(exc).__name__,
                            error=str(exc) or "(empty)",
                        ).to_payload(),
                    ),
                    extra_sinks=extra_sinks,
                )
        finally:
            if should_close_runtime:
                await review_runtime.aclose()

    async def _mark_review_result_from_outcome(
        self,
        *,
        session_id: str,
        run_id: str,
        outcome: Any,
    ) -> None:
        await self._state_store.mark_review_result(
            session_id=session_id,
            run_id=run_id,
            status=str(outcome.write_status or "written"),
            nutrient_ids=self._written_nutrient_ids(outcome),
        )

    @staticmethod
    def _written_nutrient_ids(outcome: Any) -> tuple[str, ...]:
        write_data = getattr(outcome, "write_data", None)
        if not isinstance(write_data, dict):
            return ()
        raw_ids = write_data.get("written_nutrient_ids", ())
        if not isinstance(raw_ids, (list, tuple)):
            return ()
        return tuple(item for item in raw_ids if isinstance(item, str))

    @staticmethod
    def _child_result_parts(outcome: Any) -> tuple[str, str, Any]:
        child_result = getattr(outcome, "result", None)
        run_id = getattr(child_result, "run_id", "")
        status = getattr(child_result, "status", "completed")
        error = getattr(child_result, "error", None)
        return (
            run_id if isinstance(run_id, str) else "",
            status if isinstance(status, str) else "completed",
            error,
        )

    async def _emit_event(
        self,
        event: Event,
        *,
        extra_sinks: Sequence[EventSink] = (),
    ) -> None:
        await self._event_bus.emit(event)
        for sink in extra_sinks:
            try:
                await sink.emit(event)
            except Exception:
                logger.exception("evolution event sink failed: kind=%s", event.kind)

    def _build_stub_parent_runtime(self) -> Any:
        """构造 ad-hoc SessionEngine 当 reviewer 的 parent 容器。"""
        from core.agent_spec import AgentSpec
        from core.session import InMemorySession
        from evolution.reviewer_runtime import (
            REVIEWER_TOOL_NAME,
            EvolutionApprovalMode,
            RestrictedBypassApproval,
        )
        from infrastructure.config.model_catalog_manager import ModelCatalogManager
        from runtime_assembly.session_engine import SessionEngine

        learning = self._learning
        catalog_manager = ModelCatalogManager(
            user_path=self._kongming_home / "model-providers.yaml"
        )
        resolved_model = catalog_manager.resolve_runtime(
            self._config.model,
            preset_id=learning.preset_id,
            reasoning_effort=learning.reasoning_effort,
        )

        stub_spec = AgentSpec(
            name="evolution-stub-parent",
            instructions="",
            default_model=resolved_model.name,
            tool_names=(REVIEWER_TOOL_NAME,),
            max_turns=1,
            metadata={"evolution_role": "stub_parent"},
            reasoning_effort=coerce_reasoning_effort(learning.reasoning_effort),
        )
        return SessionEngine.build(
            self._config,
            event_sinks=[self._event_bus],
            approval=RestrictedBypassApproval(
                mode=EvolutionApprovalMode.RESTRICTED_BYPASS,
            ),
            tools=self._mini_registry,
            enabled_tool_names=[REVIEWER_TOOL_NAME],
            session_factory=lambda sid: InMemorySession(session_id=sid),
            agent_spec=stub_spec,
            model_catalog_manager=catalog_manager,
            model_config=resolved_model,
        )
