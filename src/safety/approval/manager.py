"""跨宿主统一人工审批管理器。

Manager 持有 pending Future、超时任务与事件 fan-out。三步安全引擎把 danger、
remember candidate、顶层 thread_id 和 revision 写入请求元数据；本模块在 pending
创建时冻结这些值，并在 resolve 时经 PermissionsManager 执行 allow/deny 记忆写回。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.clock import now_epoch_ms
from core.contracts import ApprovalAction, ApprovalDecision, ApprovalRequest
from infrastructure.config.paths import get_kongming_home
from safety.approval.events import PendingApprovalView
from safety.approval.llm_reviewer import ApprovalLlmReviewer, LlmReviewDecision
from safety.approval.permissions_errors import PermissionsError
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.rule_models import RememberRule, Verdict
from safety.approval.types import ApprovalMetadataKeys
from safety.auto_approval.disposition import (
    ApprovalDispositionMode,
)
from safety.auto_approval.policy import AutoApprovalPolicy

logger = logging.getLogger(__name__)


@dataclass
class _PendingApproval:
    """Manager 私有的单条 pending 状态。"""

    request_id: str
    channel: str
    thread_id: str
    agent_id: str
    cwd: str
    tool_name: str
    tool_input: dict[str, Any]
    metadata: dict[str, Any]
    severity: str
    matched_rule: str | None
    danger: bool
    remember_allowed: bool
    remember_rule: RememberRule | None
    remember_revision: int | None
    future: asyncio.Future[ApprovalDecision]
    auto_approve_at_ms: int | None = None
    arrived_at_ms: int = field(default_factory=now_epoch_ms)
    timeout_ms: int = 60_000


class ApprovalEventSink(Protocol):
    """审批 pending 生命周期的宿主事件出口。"""

    async def emit_approval_required(self, *, pending: PendingApprovalView) -> None:
        """发布一条新 pending。"""
        ...

    async def emit_approval_removed(self, *, request_id: str, reason: str) -> None:
        """发布一条 pending 移除事件。"""
        ...


class ApprovalAuditSink(Protocol):
    """LLM 倒计时放行的宿主审计出口。"""

    def log_llm_auto_allow(
        self,
        *,
        channel: str,
        thread_id: str,
        request_id: str,
        cwd: str,
        mode: str,
        tool_name: str,
        matched_rule: str | None,
        model: str,
        reason: str,
        timeout_ms: int,
    ) -> None:
        """写入一条脱敏 LLM 放行审计事件。"""
        ...


class ApprovalManager:
    """持有跨通道 pending 状态并执行 thread permissions 记忆写回。"""

    def __init__(
        self,
        *,
        permissions_manager: PermissionsManager,
        event_sinks: list[ApprovalEventSink] | None = None,
        default_timeout_ms: int = 60_000,
        auto_approval_policy: AutoApprovalPolicy | None = None,
        llm_reviewer: ApprovalLlmReviewer | None = None,
        audit_sink: ApprovalAuditSink | None = None,
    ) -> None:
        """绑定 permissions 门户、事件出口和失败关闭超时。"""
        if default_timeout_ms <= 0:
            raise ValueError("default_timeout_ms must be positive")
        self._permissions = permissions_manager
        self._event_sinks: list[ApprovalEventSink] = list(event_sinks or [])
        self._default_timeout_ms = default_timeout_ms
        self._auto_approval_policy = auto_approval_policy
        self._llm_reviewer = llm_reviewer
        self._audit_sink = audit_sink
        self._pending: dict[str, _PendingApproval] = {}
        self._timeout_tasks: dict[str, asyncio.Task[None]] = {}
        self._auto_approve_tasks: dict[str, asyncio.Task[None]] = {}
        self._llm_review_tasks: dict[str, asyncio.Task[None]] = {}
        self._resolving: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def permissions_manager(self) -> PermissionsManager:
        """返回审批链与 Web REST 共享的 permissions 门户。"""
        return self._permissions

    def register_event_sink(self, sink: ApprovalEventSink) -> None:
        """在装配期注册一个审批事件接收器。"""
        self._event_sinks.append(sink)

    def has_event_sink_type(self, sink_type: type[object]) -> bool:
        """判断指定类型的事件接收器是否已经注册。"""
        return any(isinstance(sink, sink_type) for sink in self._event_sinks)

    @property
    def pending_count(self) -> int:
        """返回当前等待处理的审批数量。"""
        return len(self._pending)

    def pending_count_for_thread(self, thread_id: str, *, channel: str | None = None) -> int:
        """统计目标 thread 在可选通道内的 pending 数。"""
        if not thread_id:
            return 0
        return sum(
            1
            for pending in self._pending.values()
            if pending.thread_id == thread_id
            and (channel is None or pending.channel == channel)
            and not pending.future.done()
        )

    def has_pending_for_thread(self, thread_id: str, *, channel: str | None = None) -> bool:
        """判断目标 thread 是否存在 pending。"""
        return self.pending_count_for_thread(thread_id, channel=channel) > 0

    @property
    def timeout_task_count(self) -> int:
        """返回当前超时任务数量。"""
        return len(self._timeout_tasks)

    @property
    def auto_approve_task_count(self) -> int:
        """返回 LLM allow 后仍处于可中断窗口的倒计时任务数量。"""
        return len(self._auto_approve_tasks)

    @property
    def llm_review_task_count(self) -> int:
        """返回当前运行中的 LLM 复核任务数量。"""
        return len(self._llm_review_tasks)

    async def aclose(self) -> None:
        """取消 pending、后台任务并释放 reviewer 持有的连接池。"""
        request_ids = tuple(self._pending)
        for request_id in request_ids:
            self.cancel(request_id, reason="manager_shutdown")
        async with self._lock:
            tasks = tuple(
                task
                for task in (
                    *self._timeout_tasks.values(),
                    *self._auto_approve_tasks.values(),
                    *self._llm_review_tasks.values(),
                )
                if task is not asyncio.current_task() and not task.done()
            )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._llm_reviewer is not None:
            await self._llm_reviewer.aclose()

    async def request(
        self,
        *,
        channel: str,
        thread_id: str,
        cwd: str,
        tool_name: str,
        tool_input: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
        agent_id: str = "",
    ) -> ApprovalDecision:
        """创建 pending，发布展示事件并等待 resolve、cancel 或 timeout。"""
        if not thread_id.strip():
            raise ValueError("approval request requires a stable thread_id")
        actual_timeout_ms = timeout_ms or self._default_timeout_ms
        if actual_timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")

        request_metadata = dict(metadata or {})
        danger = request_metadata.get(ApprovalMetadataKeys.DANGER) is True
        remember_rule = _remember_rule_from_metadata(request_metadata)
        remember_thread_id = _optional_nonblank_string(
            request_metadata.get(ApprovalMetadataKeys.REMEMBER_THREAD_ID)
        )
        remember_revision = _optional_revision(
            request_metadata.get(ApprovalMetadataKeys.REMEMBER_REVISION)
        )
        remember_flag = request_metadata.get(ApprovalMetadataKeys.REMEMBER_ALLOWED) is True
        remember_allowed = (
            remember_flag
            and not danger
            and remember_rule is not None
            and remember_thread_id == thread_id
            and remember_revision is not None
        )
        if remember_flag and not remember_allowed:
            logger.warning(
                "approval remember context rejected during pending freeze request_thread=%s frozen_thread=%s",
                thread_id,
                remember_thread_id,
            )

        loop = asyncio.get_running_loop()
        request_id = uuid.uuid4().hex
        pending = _PendingApproval(
            request_id=request_id,
            channel=channel,
            thread_id=thread_id,
            agent_id=agent_id,
            cwd=cwd,
            tool_name=tool_name,
            tool_input=dict(tool_input),
            metadata=request_metadata,
            severity="danger" if danger else _severity_from_metadata(request_metadata),
            matched_rule=_optional_nonblank_string(
                request_metadata.get(ApprovalMetadataKeys.MATCHED_RULE)
            ),
            danger=danger,
            remember_allowed=remember_allowed,
            remember_rule=remember_rule if remember_allowed else None,
            remember_revision=remember_revision if remember_allowed else None,
            future=loop.create_future(),
            timeout_ms=actual_timeout_ms,
        )

        async with self._lock:
            self._pending[request_id] = pending
            self._timeout_tasks[request_id] = asyncio.create_task(
                self._handle_timeout(request_id, actual_timeout_ms / 1000.0)
            )
            sinks = list(self._event_sinks)

        await asyncio.gather(
            *(sink.emit_approval_required(pending=_pending_to_view(pending)) for sink in sinks),
            return_exceptions=True,
        )
        if self._is_llm_review_eligible(pending):
            async with self._lock:
                current = self._pending.get(request_id)
                if current is not None and not current.future.done():
                    self._llm_review_tasks[request_id] = asyncio.create_task(
                        self._handle_llm_review(request_id)
                    )
        try:
            decision = await pending.future
            source = decision.metadata.get("source")
            reason = "timeout" if source == "manager_timeout" else "user_decided"
            if source == "manager_cancel":
                reason = "cancelled"
            elif source == "llm_auto_allow":
                reason = "auto_allowed"
            await self._cleanup_pending(request_id, fan_out_reason=reason)
            return decision
        except BaseException:
            await self._cleanup_pending(request_id, fan_out_reason="cancelled")
            raise

    async def resolve(
        self,
        thread_id: str,
        request_id: str,
        decision: Mapping[str, object],
    ) -> bool:
        """校验 pending 身份，可选写本子，再完成用户决定。"""
        async with self._lock:
            pending = self._pending.get(request_id)
            if (
                pending is None
                or pending.thread_id != thread_id
                or pending.future.done()
                or request_id in self._resolving
            ):
                return False
            allow = decision.get("allow")
            remember = decision.get("remember", False)
            if not isinstance(allow, bool) or not isinstance(remember, bool):
                return False
            if remember and not pending.remember_allowed:
                return False
            if remember and not _remember_rule_matches_decision(
                pending.remember_rule,
                decision.get("rememberRule"),
            ):
                return False
            self._resolving.add(request_id)
            timeout_task = self._timeout_tasks.pop(request_id, None)
        if timeout_task is not None and not timeout_task.done():
            timeout_task.cancel()

        if remember:
            saved = await self._write_remembered_decision(pending, allow=allow)
            if not saved:
                await self._restore_after_failed_resolve(pending)
                return False

        metadata: dict[str, Any] = {
            "source": "user",
            "decided_by": "user",
            "matched_rule": pending.matched_rule,
            "remembered": remember,
            ApprovalMetadataKeys.DANGER: pending.danger,
        }
        message = decision.get("message")
        if isinstance(message, str) and message:
            metadata["message"] = message
        if remember and pending.remember_rule is not None:
            metadata[ApprovalMetadataKeys.REMEMBER_RULE] = {
                "expression": pending.remember_rule.expression,
                "displayText": pending.remember_rule.display_text,
                "scopeCwd": pending.remember_rule.scope_cwd,
            }
            metadata[ApprovalMetadataKeys.REMEMBER_THREAD_ID] = pending.thread_id

        async with self._lock:
            self._resolving.discard(request_id)
            if pending.future.done():
                return False
            pending.future.set_result(
                ApprovalDecision(
                    outcome="approved" if allow else "rejected",
                    metadata=metadata,
                )
            )
        return True

    def cancel(self, request_id: str, reason: str = "cancelled") -> bool:
        """取消一条尚未进入 remember 写入阶段的 pending。"""
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done() or request_id in self._resolving:
            return False
        pending.future.set_result(
            ApprovalDecision(
                outcome="rejected",
                metadata={"source": "manager_cancel", "reason": reason},
            )
        )
        return True

    def cancel_by_thread(self, thread_id: str, reason: str = "cancelled") -> int:
        """取消目标 thread 当前可取消的全部 pending。"""
        targets = [
            request_id
            for request_id, pending in self._pending.items()
            if pending.thread_id == thread_id and not pending.future.done()
        ]
        return sum(self.cancel(request_id, reason=reason) for request_id in targets)

    def cancel_by_agent(self, agent_id: str, reason: str = "cancelled") -> int:
        """取消目标 agent 当前可取消的全部 pending。"""
        targets = [
            request_id
            for request_id, pending in self._pending.items()
            if pending.agent_id == agent_id and not pending.future.done()
        ]
        return sum(self.cancel(request_id, reason=reason) for request_id in targets)

    async def _write_remembered_decision(
        self,
        pending: _PendingApproval,
        *,
        allow: bool,
    ) -> bool:
        """使用 pending 冻结的 candidate 与 revision 写入目标 thread。"""
        if pending.remember_rule is None or pending.remember_revision is None:
            return False
        verdict = Verdict.ALLOW if allow else Verdict.DENY
        try:
            entry = self._permissions.build_entry(
                pending.remember_rule.expression,
                verdict,
                scope_cwd=pending.remember_rule.scope_cwd,
            )
            await self._permissions.write_entry(
                pending.thread_id,
                entry,
                expected_revision=pending.remember_revision,
            )
        except (PermissionsError, ValueError):
            logger.exception(
                "approval remember write failed request_id=%s thread_id=%s",
                pending.request_id,
                pending.thread_id,
            )
            return False
        return True

    async def _restore_after_failed_resolve(self, pending: _PendingApproval) -> None:
        """保存失败后恢复可重试状态与 fail-closed 超时任务。"""
        async with self._lock:
            self._resolving.discard(pending.request_id)
            if pending.future.done() or pending.request_id not in self._pending:
                return
            self._timeout_tasks[pending.request_id] = asyncio.create_task(
                self._handle_timeout(
                    pending.request_id,
                    pending.timeout_ms / 1000.0,
                )
            )

    async def _handle_timeout(self, request_id: str, timeout_seconds: float) -> None:
        """到时后以拒绝结果关闭仍未处理的 pending。"""
        try:
            await asyncio.sleep(timeout_seconds)
        except asyncio.CancelledError:
            return
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done() or request_id in self._resolving:
            return
        pending.future.set_result(
            ApprovalDecision(
                outcome="rejected",
                metadata={"source": "manager_timeout", "reason": "timeout"},
            )
        )

    def _is_llm_review_eligible(self, pending: _PendingApproval) -> bool:
        """仅让 llm 模式的 default:ask 进入复核任务。"""
        if self._llm_reviewer is None or self._auto_approval_policy is None:
            return False
        if pending.matched_rule != "default:ask" or pending.danger:
            return False
        try:
            return self._auto_approval_policy.mode_for(pending.cwd) is ApprovalDispositionMode.LLM
        except Exception:
            logger.exception("approval disposition lookup failed cwd=%s", pending.cwd)
            return False

    async def _handle_llm_review(self, request_id: str) -> None:
        """运行 LLM；allow 后广播同一 request_id 的倒计时更新。"""
        reviewer = self._llm_reviewer
        policy = self._auto_approval_policy
        if reviewer is None or policy is None:
            return
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            return
        try:
            result = await reviewer.review(
                cwd=pending.cwd,
                tool_name=pending.tool_name,
                tool_input=pending.tool_input,
                matched_rule=pending.matched_rule or "default:ask",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("approval llm review failed request_id=%s", request_id)
            return
        if result.decision is not LlmReviewDecision.ALLOW:
            return
        try:
            if policy.mode_for(pending.cwd) is not ApprovalDispositionMode.LLM:
                return
        except Exception:
            logger.exception("approval disposition recheck failed request_id=%s", request_id)
            return

        countdown_ms = self._llm_countdown_ms(pending.cwd)
        async with self._lock:
            current = self._pending.get(request_id)
            if current is None or current.future.done():
                return
            current.auto_approve_at_ms = now_epoch_ms() + countdown_ms
            current.metadata["llm_review"] = {
                "model": result.model,
                "reason": result.reason,
                "decision": result.decision.value,
            }
            self._auto_approve_tasks[request_id] = asyncio.create_task(
                self._handle_auto_approve(request_id, countdown_ms / 1000.0)
            )
            sinks = list(self._event_sinks)

        self._record_llm_auto_allow(current, result.model, result.reason, countdown_ms)
        await asyncio.gather(
            *(sink.emit_approval_required(pending=_pending_to_view(current)) for sink in sinks),
            return_exceptions=True,
        )

    def _llm_countdown_ms(self, cwd: str) -> int:
        """从 per-cwd policy 读取倒计时，异常时采用全局规则默认值。"""
        policy = self._auto_approval_policy
        if policy is None:
            return self._default_timeout_ms
        try:
            config = policy.get_config(cwd)
            rule_set = policy.rule_set
            return int(config.timeout_ms or rule_set.default_timeout_ms)
        except Exception:
            logger.exception("approval countdown lookup failed cwd=%s", cwd)
            return self._default_timeout_ms

    def _record_llm_auto_allow(
        self,
        pending: _PendingApproval,
        model: str,
        reason: str,
        timeout_ms: int,
    ) -> None:
        """写入 LLM allow 进入倒计时的独立审计，异常不影响主链。"""
        if self._audit_sink is None:
            return
        try:
            self._audit_sink.log_llm_auto_allow(
                channel=pending.channel,
                thread_id=pending.thread_id,
                request_id=pending.request_id,
                cwd=pending.cwd,
                mode=ApprovalDispositionMode.LLM.value,
                tool_name=pending.tool_name,
                matched_rule=pending.matched_rule,
                model=model,
                reason=reason,
                timeout_ms=timeout_ms,
            )
        except Exception:
            logger.exception(
                "approval llm auto-allow audit failed request_id=%s", pending.request_id
            )

    async def _handle_auto_approve(self, request_id: str, delay_seconds: float) -> None:
        """等待用户可中断窗口结束后放行已获 LLM allow 的 pending。"""
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done() or request_id in self._resolving:
            return
        pending.future.set_result(
            ApprovalDecision(
                outcome="approved",
                metadata={
                    "source": "llm_auto_allow",
                    "reason": "auto_allow",
                    "matched_rule": pending.matched_rule,
                    "llm_review": pending.metadata.get("llm_review"),
                },
            )
        )

    async def _cleanup_pending(self, request_id: str, *, fan_out_reason: str) -> None:
        """统一删除 pending、超时任务和 resolving 标记并广播移除。"""
        async with self._lock:
            self._pending.pop(request_id, None)
            self._resolving.discard(request_id)
            timeout_task = self._timeout_tasks.pop(request_id, None)
            auto_approve_task = self._auto_approve_tasks.pop(request_id, None)
            llm_review_task = self._llm_review_tasks.pop(request_id, None)
            sinks = list(self._event_sinks)
        if timeout_task is not None and not timeout_task.done():
            timeout_task.cancel()
        if auto_approve_task is not None and not auto_approve_task.done():
            auto_approve_task.cancel()
        if llm_review_task is not None and not llm_review_task.done():
            llm_review_task.cancel()
        await asyncio.gather(
            *(
                sink.emit_approval_removed(
                    request_id=request_id,
                    reason=fan_out_reason,
                )
                for sink in sinks
            ),
            return_exceptions=True,
        )


_singleton: ApprovalManager | None = None


def get_approval_manager(
    *,
    permissions_manager: PermissionsManager | None = None,
    event_sinks: list[ApprovalEventSink] | None = None,
    default_timeout_ms: int = 60_000,
    auto_approval_policy: AutoApprovalPolicy | None = None,
    llm_reviewer: ApprovalLlmReviewer | None = None,
    audit_sink: ApprovalAuditSink | None = None,
) -> ApprovalManager:
    """获取或创建进程级审批管理器单例。"""
    global _singleton
    if _singleton is None:
        _singleton = ApprovalManager(
            permissions_manager=permissions_manager or PermissionsManager(get_kongming_home()),
            event_sinks=event_sinks,
            default_timeout_ms=default_timeout_ms,
            auto_approval_policy=auto_approval_policy,
            llm_reviewer=llm_reviewer,
            audit_sink=audit_sink,
        )
    return _singleton


def reset_for_testing() -> None:
    """仅供测试清除进程级单例。"""
    global _singleton
    _singleton = None


def make_manager_prompt_fn(
    manager: ApprovalManager,
    thread_id: str,
    *,
    channel: str = "generic_chat",
    default_cwd: str = "",
) -> Callable[[ApprovalRequest], Awaitable[ApprovalAction]]:
    """生成绑定顶层 thread 的 InteractiveApproval 动作提示函数。"""

    async def prompt_fn(request: ApprovalRequest) -> ApprovalAction:
        """将运行时请求提交 Manager，并映射为工具运行时 action。"""
        raw_cwd = request.execution_scope.cwd
        metadata_cwd = request.metadata.get("cwd")
        cwd = (
            raw_cwd
            if isinstance(raw_cwd, str) and raw_cwd
            else metadata_cwd
            if isinstance(metadata_cwd, str) and metadata_cwd
            else default_cwd
        )
        metadata = dict(request.metadata)
        metadata.setdefault("run_id", request.run_id)
        metadata.setdefault("session_id", request.session_id)
        metadata.setdefault("turn", request.turn)
        metadata.setdefault("call_id", request.call_id)
        if request.reason:
            metadata.setdefault("reason", request.reason)
        agent_id = _agent_id_from_metadata(metadata)
        decision = await manager.request(
            channel=channel,
            thread_id=thread_id,
            cwd=cwd,
            tool_name=request.tool_name,
            tool_input=dict(request.arguments),
            metadata=metadata,
            agent_id=agent_id,
        )
        return _decision_to_action(decision)

    prompt_fn.__action_aware__ = True  # type: ignore[attr-defined]
    return prompt_fn


def _decision_to_action(decision: ApprovalDecision) -> ApprovalAction:
    """把 Manager 最终决定映射为工具运行时一次性 action。"""
    if decision.outcome == "approved":
        return ApprovalAction.ACCEPT_ONCE
    return ApprovalAction.REJECT


def _pending_to_view(pending: _PendingApproval) -> PendingApprovalView:
    """将私有 pending 投影为宿主可读 DTO。"""
    return PendingApprovalView(
        request_id=pending.request_id,
        channel=pending.channel,
        thread_id=pending.thread_id,
        agent_id=pending.agent_id,
        cwd=pending.cwd,
        tool_name=pending.tool_name,
        tool_input=pending.tool_input,
        metadata=pending.metadata,
        severity=pending.severity,
        matched_rule=pending.matched_rule,
        danger=pending.danger,
        remember_allowed=pending.remember_allowed,
        arrived_at_ms=pending.arrived_at_ms,
        timeout_ms=pending.timeout_ms,
        remember_rule=pending.remember_rule,
        auto_approve_at_ms=pending.auto_approve_at_ms,
    )


def _remember_rule_from_metadata(metadata: Mapping[str, Any]) -> RememberRule | None:
    """从决策引擎元数据读取冻结的记忆候选。"""
    value = metadata.get(ApprovalMetadataKeys.REMEMBER_RULE)
    if not isinstance(value, Mapping):
        return None
    expression = value.get("expression")
    display_text = value.get("displayText")
    scope_cwd = value.get("scopeCwd")
    if (
        not isinstance(expression, str)
        or not isinstance(display_text, str)
        or (scope_cwd is not None and not isinstance(scope_cwd, str))
    ):
        return None
    return RememberRule(
        expression=expression,
        display_text=display_text,
        scope_cwd=scope_cwd,
    )


def _remember_rule_matches_decision(
    frozen: RememberRule | None,
    claimed: object,
) -> bool:
    """严格比较客户端回传与服务端 pending 候选，阻止 scope 篡改和缺失。"""
    if frozen is None or not isinstance(claimed, Mapping):
        return False
    return (
        set(claimed) == {"expression", "displayText", "scopeCwd"}
        and claimed.get("expression") == frozen.expression
        and claimed.get("displayText") == frozen.display_text
        and claimed.get("scopeCwd") == frozen.scope_cwd
    )


def _optional_nonblank_string(value: object) -> str | None:
    """把可选 wire 值收敛为非空字符串。"""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _optional_revision(value: object) -> int | None:
    """把可选 wire 值收敛为非负 revision。"""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _severity_from_metadata(metadata: Mapping[str, Any]) -> str:
    """读取普通审批展示强度，缺省返回 standard。"""
    severity = metadata.get("severity")
    if isinstance(severity, str) and severity:
        return severity
    return "standard"


def _agent_id_from_metadata(metadata: Mapping[str, Any]) -> str:
    """优先读取 agent_id，随后读取 parent_agent 展示身份。"""
    agent_id = metadata.get("agent_id")
    if isinstance(agent_id, str):
        return agent_id
    parent = metadata.get("parent_agent")
    if isinstance(parent, Mapping):
        parent_agent_id = parent.get("agent_id")
        if isinstance(parent_agent_id, str):
            return parent_agent_id
    return ""


__all__ = [
    "ApprovalAuditSink",
    "ApprovalEventSink",
    "ApprovalManager",
    "PendingApprovalView",
    "_decision_to_action",
    "get_approval_manager",
    "make_manager_prompt_fn",
    "reset_for_testing",
]
