"""v0.6 高层安全链装配。

本模块向 Runner 暴露 SafetyGatedApproval，内部只装配 DangerGuard、全局模式、
PermissionsManager、ConsentResolver 和事件 fan-out。thread 本子与全局 config
各有单一 owner，旧 capability/permission/boundary/grant 链不再参与运行。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from core.contracts import ApprovalDecision, ApprovalProvider, ApprovalRequest
from core.errors import AgentError
from infrastructure.config.models import Config
from infrastructure.config.paths import get_kongming_home
from safety.approval.decision_engine import SafetyDecisionEngine, TraceEmitter
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.types import ApprovalMetadataKeys
from safety.auto_approval.disposition import ApprovalDispositionResolver
from safety.auto_approval.manager import AutoApprovalManager
from safety.guards.consent import ConsentResolver
from safety.guards.danger import DangerGuard

if TYPE_CHECKING:
    from core.contracts import Event, EventSink


class SafetyChainError(AgentError):
    """安全链自身发生未预期异常。"""


class SafetyGatedApproval:
    """实现 ApprovalProvider 的 v0.6 安全链门面。"""

    def __init__(self, *, engine: SafetyDecisionEngine) -> None:
        """绑定唯一决策引擎。"""
        self._engine = engine

    def with_interactive_approval(
        self,
        interactive_approval: ApprovalProvider,
    ) -> ApprovalProvider:
        """保留 DangerGuard、模式、thread permissions 与事件链，替换人工终点。"""
        return SafetyGatedApproval(
            engine=self._engine.with_interactive_approval(interactive_approval)
        )

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """委托三步决策引擎，并把内部异常收敛为 SafetyChainError。"""
        try:
            return await self._engine.decide(request)
        except Exception as exc:
            raise SafetyChainError(
                "SafetyDecisionEngine raised unexpectedly",
                details={
                    "tool_name": request.tool_name,
                    "call_id": request.call_id,
                    "error": repr(exc),
                },
            ) from exc


def build_safety_chain(
    config: Config,
    *,
    interactive_approval: ApprovalProvider,
    permissions_manager: PermissionsManager | None = None,
    danger_guard: DangerGuard | None = None,
    trace_emitter: TraceEmitter | None = None,
    event_sinks: list[EventSink] | None = None,
    disposition_resolver: ApprovalDispositionResolver | None = None,
) -> SafetyGatedApproval:
    """按统一 config 和 Kongming home 装配完整 v0.6 安全链。"""
    kongming_home = get_kongming_home()
    permissions = permissions_manager or PermissionsManager(kongming_home)
    danger = danger_guard or DangerGuard(kongming_home=kongming_home)
    consent = ConsentResolver(interactive_approval=interactive_approval)
    resolver = disposition_resolver or AutoApprovalManager.build(kongming_home).policy

    resolved_emitter = trace_emitter
    if resolved_emitter is None and event_sinks:
        resolved_emitter = _build_event_sink_emitter(event_sinks=event_sinks)

    return SafetyGatedApproval(
        engine=SafetyDecisionEngine(
            danger_guard=danger,
            disposition_resolver=resolver,
            permissions_manager=permissions,
            consent=consent,
            trace_emitter=resolved_emitter,
        )
    )


_PENDING_EMIT_TASKS: set[asyncio.Task[None]] = set()


def _build_event_sink_emitter(*, event_sinks: list[EventSink]) -> TraceEmitter:
    """构造同步 trace callback，并把事件异步 fan-out 到全部 EventSink。"""
    from core.contracts import Event

    def _emit(
        event_kind: str,
        decision: ApprovalDecision,
        request: ApprovalRequest,
    ) -> None:
        """把一条决策转换为 Event 并调度到所有 sink。"""
        event = Event(
            kind=event_kind,
            run_id=request.run_id,
            turn=request.turn,
            payload=_build_event_payload(decision, request),
        )
        for sink in event_sinks:
            _safely_dispatch_emit(sink, event)

    return _emit


def _safely_dispatch_emit(sink: EventSink, event: Event) -> None:
    """调度单个 sink.emit，隔离观测失败与主决策链。"""
    coro = sink.emit(event)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        with contextlib.suppress(Exception):
            asyncio.run(coro)
        return
    task = loop.create_task(coro)
    _PENDING_EMIT_TASKS.add(task)
    task.add_done_callback(_discard_emit_task)


def _discard_emit_task(task: asyncio.Task[None]) -> None:
    """从模块级任务集合移除已结束的 emit task。"""
    _PENDING_EMIT_TASKS.discard(task)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


def _build_event_payload(
    decision: ApprovalDecision,
    request: ApprovalRequest,
) -> dict[str, Any]:
    """投影安全决策审计字段和工具目标。"""
    metadata = decision.metadata
    payload: dict[str, Any] = {
        "decision_class": metadata.get(ApprovalMetadataKeys.DECISION_CLASS),
        "decision_source": metadata.get(ApprovalMetadataKeys.DECISION_SOURCE),
        "matched_rule": metadata.get(ApprovalMetadataKeys.MATCHED_RULE),
        "reason": metadata.get(ApprovalMetadataKeys.REASON) or decision.reason,
        "boundary_kind": metadata.get(ApprovalMetadataKeys.BOUNDARY_KIND, "host"),
        "danger": metadata.get(ApprovalMetadataKeys.DANGER, False),
        "remember_allowed": metadata.get(ApprovalMetadataKeys.REMEMBER_ALLOWED, False),
        "tool_name": request.tool_name,
        "path_or_command": _extract_path_or_command(request),
        "request_id": request.call_id,
        "outcome": decision.outcome,
        "audit_priority": metadata.get("audit_priority", "normal"),
        "execution_scope_cwd": request.execution_scope.cwd,
        "matched_rule_scope_cwd": metadata.get("matched_rule_scope_cwd"),
    }
    if request.tool_name == "run_shell":
        command = request.arguments.get("command")
        if isinstance(command, str):
            payload["command"] = command
    return payload


def _extract_path_or_command(request: ApprovalRequest) -> str | None:
    """从工具参数提取最适合审计的 path 或 command。"""
    path = request.arguments.get("path", request.arguments.get("file_path"))
    if isinstance(path, str):
        return path
    command = request.arguments.get("command")
    if isinstance(command, str):
        return command
    return None


__all__ = [
    "SafetyChainError",
    "SafetyGatedApproval",
    "build_safety_chain",
]
