"""唯一 run loop 真身。

这里是 v1-mini 里 **仅有的** turn 推进入口。``native_runtime.py``、
``session.py``、``run_state.py`` 都不允许再长出第二套 loop。

职责边界：

- 负责 turn 推进、tool_call 回填、停止条件、结果收口
- 负责把关键节点事件 fan-out 到 ``list[EventSink]``
- 负责把异常包成 :class:`core.errors.AgentError` 子类

不负责：

- 装配 provider / tools / session（那是 ``executors/agent_runtime/native_runtime.py`` 的事）
- safety 判定（capability / permission 由装配层预先串好，runner 只消费 ApprovalProvider）
- 具体 provider 调用或工具执行（通过协议委托）
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from core.agent_spec import AgentSpec
from core.contracts import (
    ApprovalProvider,
    ApprovalRequest,
    Event,
    EventSink,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    MessageCompactor,
    Session,
    Tool,
    ToolContext,
    ToolLookup,
    ToolResult,
)
from core.errors import (
    AgentError,
    MaxTurnsExceededError,
    ProviderError,
    ToolError,
)
from core.lifecycle import LifecycleHook
from core.message import Message, ToolCall
from core.result import Result
from core.run_state import RunState


class Runner:
    """唯一 run loop。

    构造时只拿 ``event_sinks`` 和可选 ``lifecycle_hooks``；每次 :meth:`run`
    调用接 per-run 的依赖。装配层负责替每次调用拼齐参数。

    这个类是无状态的（run 相关状态都放在 per-call 的 :class:`RunState` 里），
    因此可以安全地被复用于多次 run。
    """

    def __init__(
        self,
        *,
        event_sinks: Sequence[EventSink] | None = None,
        lifecycle_hooks: Sequence[LifecycleHook] | None = None,
        message_compactor: MessageCompactor | None = None,
    ) -> None:
        self._event_sinks: list[EventSink] = list(event_sinks or [])
        self._lifecycle_hooks: list[LifecycleHook] = list(lifecycle_hooks or [])
        # 可选：把 history 送给 LLM 之前过一道 compactor；None 表示原样透传。
        self._message_compactor: MessageCompactor | None = message_compactor

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    async def run(
        self,
        user_input: str,
        *,
        session: Session,
        agent_spec: AgentSpec,
        llm: LLMProvider,
        tools: ToolLookup,
        approval: ApprovalProvider,
        max_turns: int | None = None,
        run_id: str | None = None,
        enabled_tools: Sequence[Tool] | None = None,
    ) -> Result:
        """执行一次完整 run。

        Args:
            user_input: 用户输入文本；会被包成 user Message 追加到 session。
            session: 会话存储。runner 不假设实现，只要满足 Session Protocol。
            agent_spec: 本次运行使用的 agent 规格，提供 system prompt / 默认 model。
            llm: LLM provider，负责真实模型调用。
            tools: 工具查找面，按 ``agent_spec.tool_names`` 解析。
                也接受 ``Mapping[str, Tool]``（如 dict），因为结构上满足 ToolLookup。
            approval: 审批 provider。所有工具调用都走它，即使策略是 auto-approve。
            max_turns: 覆盖 agent_spec.max_turns；None 表示使用 spec 默认。
            run_id: 外部可以注入 run_id 便于跨进程关联；None 时 runner 自生成。
            enabled_tools: 允许装配层直接传"已解析好的 Tool 列表"绕过 tool_names 查询。
                若提供，runner 不再按 spec.tool_names 去 tools 里查，避免重复。

        Returns:
            :class:`Result`：运行结束的统一结果。

        Raises:
            不直接抛异常；所有异常都收口到 Result.error 与 status=failed。
            这是为了让 host / cli 只关心 Result 即可。
        """

        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        effective_max_turns = max_turns if max_turns is not None else agent_spec.max_turns

        state = RunState(run_id=run_id, session_id=session.session_id)
        state.mark_running()

        await self._emit(
            Event(
                kind="run.start",
                run_id=run_id,
                payload={
                    "session_id": session.session_id,
                    "agent": agent_spec.name,
                    "model": agent_spec.default_model,
                    "max_turns": effective_max_turns,
                },
            )
        )

        try:
            resolved_tools = self._resolve_tools(agent_spec, tools, enabled_tools)
            await self._seed_messages(session, agent_spec, user_input, state)
            final_message = await self._drive_turns(
                state=state,
                session=session,
                agent_spec=agent_spec,
                llm=llm,
                tools_by_name={t.name: t for t in resolved_tools},
                resolved_tools=resolved_tools,
                approval=approval,
                max_turns=effective_max_turns,
            )
            state.mark_completed()
            result = Result(
                run_id=run_id,
                session_id=session.session_id,
                status="completed",
                final_message=final_message,
                turn_count=state.turn,
            )
        except AgentError as exc:
            state.mark_failed(exc)
            await self._emit(
                Event(
                    kind="error",
                    run_id=run_id,
                    turn=state.turn,
                    payload={"type": type(exc).__name__, "message": exc.message},
                )
            )
            result = Result(
                run_id=run_id,
                session_id=session.session_id,
                status="failed",
                final_message=None,
                turn_count=state.turn,
                error=exc,
            )
        except Exception as exc:  # pragma: no cover - 意外异常兜底
            wrapped = AgentError(
                f"unexpected error in runner: {exc!r}",
                details={"exception_type": type(exc).__name__},
            )
            state.mark_failed(wrapped)
            await self._emit(
                Event(
                    kind="error",
                    run_id=run_id,
                    turn=state.turn,
                    payload={"type": "UnexpectedError", "message": str(exc)},
                )
            )
            result = Result(
                run_id=run_id,
                session_id=session.session_id,
                status="failed",
                final_message=None,
                turn_count=state.turn,
                error=wrapped,
            )

        await self._emit(
            Event(
                kind="run.end",
                run_id=run_id,
                turn=state.turn,
                payload={"status": result.status, "turn_count": result.turn_count},
            )
        )
        return result

    # ------------------------------------------------------------------
    # 内部流程
    # ------------------------------------------------------------------

    def _resolve_tools(
        self,
        agent_spec: AgentSpec,
        tools: ToolLookup,
        enabled_tools: Sequence[Tool] | None,
    ) -> list[Tool]:
        """按 AgentSpec.tool_names 从 ToolLookup 中解析 Tool 列表。"""
        if enabled_tools is not None:
            return list(enabled_tools)
        resolved: list[Tool] = []
        for name in agent_spec.tool_names:
            if name not in tools:
                raise AgentError(
                    f"tool {name!r} declared in agent_spec.tool_names is not available in the "
                    "provided ToolLookup",
                    details={"tool_name": name, "agent": agent_spec.name},
                )
            resolved.append(tools[name])
        return resolved

    async def _seed_messages(
        self,
        session: Session,
        agent_spec: AgentSpec,
        user_input: str,
        state: RunState,
    ) -> None:
        """如果 session 里还没有 system 指令，就先把 AgentSpec.instructions 注入。"""
        existing = await session.history()
        has_system = any(m.role == "system" for m in existing)
        if not has_system and agent_spec.instructions:
            system_msg = Message.system(agent_spec.instructions)
            await session.append(system_msg)
            state.record(system_msg)
        user_msg = Message.user(user_input)
        await session.append(user_msg)
        state.record(user_msg)

    async def _drive_turns(
        self,
        *,
        state: RunState,
        session: Session,
        agent_spec: AgentSpec,
        llm: LLMProvider,
        tools_by_name: dict[str, Tool],
        resolved_tools: list[Tool],
        approval: ApprovalProvider,
        max_turns: int,
    ) -> Message | None:
        """核心 turn 循环。返回最终 assistant 消息。"""
        final_assistant: Message | None = None

        while True:
            if state.turn >= max_turns:
                raise MaxTurnsExceededError(
                    f"exceeded max_turns={max_turns} without reaching a terminal response",
                    details={"run_id": state.run_id, "max_turns": max_turns},
                )

            state.advance_turn()
            await self._emit(Event(kind="turn.start", run_id=state.run_id, turn=state.turn))
            await self._run_lifecycle_before_turn(state)

            history = await session.history()
            prepared_messages, compact_meta = await self._prepare_messages(history)
            if compact_meta is not None:
                await self._emit(
                    Event(
                        kind="history.compact",
                        run_id=state.run_id,
                        turn=state.turn,
                        payload=compact_meta,
                    )
                )
            llm_request = LLMRequest(
                model=agent_spec.default_model,
                messages=tuple(prepared_messages),
                tools=tuple(resolved_tools),
            )
            await self._emit(
                Event(
                    kind="llm.request",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={
                        "model": llm_request.model,
                        "message_count": len(llm_request.messages),
                        "tool_count": len(llm_request.tools),
                        "original_message_count": len(history),
                    },
                )
            )

            response = await self._safe_llm_complete(llm, llm_request)
            assistant_message = response.message
            await session.append(assistant_message)
            state.record(assistant_message)

            await self._emit(
                Event(
                    kind="llm.response",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={
                        "finish_reason": response.finish_reason,
                        "has_tool_calls": bool(assistant_message.tool_calls),
                        "usage": dict(response.usage),
                    },
                )
            )

            await self._run_lifecycle_after_turn(state, assistant_message)
            await self._emit(Event(kind="turn.end", run_id=state.run_id, turn=state.turn))

            tool_calls = assistant_message.tool_calls or ()
            if not tool_calls:
                # 视为终止：无论 finish_reason 是 stop / length / other，核心只看 tool_calls。
                final_assistant = assistant_message
                break

            # 有 tool_calls：依次执行、回填
            await self._execute_tool_calls(
                state=state,
                session=session,
                approval=approval,
                tool_calls=tool_calls,
                tools_by_name=tools_by_name,
            )
            # 回填后继续下一个 turn

        return final_assistant

    async def _prepare_messages(
        self,
        history: Sequence[Message],
    ) -> tuple[list[Message], dict[str, Any] | None]:
        """把 history 过一道 ``MessageCompactor``；没有则原样透传。

        Returns:
            ``(messages, meta)``。``meta`` 只在真正发生压缩（原始长度 != 压缩后
            长度）时为 non-None，便于 runner 决定是否 emit ``history.compact``
            事件；原样透传时返回 ``None`` 保持事件流安静。
        """
        if self._message_compactor is None:
            return list(history), None

        try:
            compacted = await self._message_compactor.compact(history)
        except Exception as exc:
            # compactor 失败不应拖垮主链路；记一条错误事件，然后原样透传。
            await self._emit(
                Event(
                    kind="error",
                    run_id="unknown",
                    payload={
                        "source": "message_compactor",
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
            )
            return list(history), None

        original = len(history)
        compacted_count = len(compacted)
        if compacted_count == original:
            return compacted, None
        meta: dict[str, Any] = {
            "original_count": original,
            "compacted_count": compacted_count,
            "dropped_count": original - compacted_count,
        }
        return compacted, meta

    async def _safe_llm_complete(self, llm: LLMProvider, request: LLMRequest) -> LLMResponse:
        """包装 provider 调用异常为 :class:`ProviderError`。"""
        try:
            return await llm.complete(request)
        except AgentError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"LLM provider call failed: {exc!r}",
                details={"exception_type": type(exc).__name__},
            ) from exc

    async def _execute_tool_calls(
        self,
        *,
        state: RunState,
        session: Session,
        approval: ApprovalProvider,
        tool_calls: Iterable[ToolCall],
        tools_by_name: dict[str, Tool],
    ) -> None:
        for call in tool_calls:
            await self._run_lifecycle_before_tool(state, call)
            await self._emit(
                Event(
                    kind="tool.call.start",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={
                        "call_id": call.call_id,
                        "tool_name": call.tool_name,
                    },
                )
            )

            tool = tools_by_name.get(call.tool_name)
            if tool is None:
                result_message = self._build_tool_error_message(
                    call,
                    f"tool {call.tool_name!r} not registered",
                )
                await session.append(result_message)
                state.record(result_message)
                await self._emit(
                    Event(
                        kind="tool.call.end",
                        run_id=state.run_id,
                        turn=state.turn,
                        payload={
                            "call_id": call.call_id,
                            "tool_name": call.tool_name,
                            "ok": False,
                            "reason": "not_registered",
                        },
                    )
                )
                await self._run_lifecycle_after_tool(state, call, result_message)
                continue

            # 审批：runner 不判 allow/deny，只咨询 ApprovalProvider
            approval_request = ApprovalRequest(
                run_id=state.run_id,
                session_id=state.session_id,
                turn=state.turn,
                call_id=call.call_id,
                tool_name=call.tool_name,
                arguments=dict(call.arguments),
            )
            await self._emit(
                Event(
                    kind="approval.request",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={"call_id": call.call_id, "tool_name": call.tool_name},
                )
            )
            previous_status = state.status
            state.mark_waiting_approval()
            decision = await approval.decide(approval_request)
            state.status = previous_status  # 恢复到之前的 running 状态
            await self._emit(
                Event(
                    kind="approval.decision",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={
                        "call_id": call.call_id,
                        "tool_name": call.tool_name,
                        "outcome": decision.outcome,
                        "reason": decision.reason,
                    },
                )
            )

            if not decision.approved:
                reason = decision.reason or decision.outcome
                rejected_msg = self._build_tool_error_message(
                    call,
                    f"approval {decision.outcome}: {reason}",
                    metadata={"approval_outcome": decision.outcome},
                )
                await session.append(rejected_msg)
                state.record(rejected_msg)
                await self._emit(
                    Event(
                        kind="tool.call.end",
                        run_id=state.run_id,
                        turn=state.turn,
                        payload={
                            "call_id": call.call_id,
                            "tool_name": call.tool_name,
                            "ok": False,
                            "reason": f"approval_{decision.outcome}",
                        },
                    )
                )
                await self._run_lifecycle_after_tool(state, call, rejected_msg)
                # 审批拒绝不直接终止 run；把"被拒绝"这条事实喂回模型，
                # 由模型决定下一步。这样 safety 的策略层可以通过文本说明 / reason
                # 让模型调整计划，而不需要立刻结束 run。
                continue

            # 真正执行工具
            ctx = ToolContext(
                run_id=state.run_id,
                session_id=state.session_id,
                turn=state.turn,
                call_id=call.call_id,
            )
            tool_result, tool_err = await self._safe_tool_execute(tool, call, ctx)
            if tool_err is not None:
                result_message = self._build_tool_error_message(
                    call,
                    tool_err.message,
                    metadata={"error_type": type(tool_err).__name__},
                )
            else:
                assert tool_result is not None  # for type checker
                result_message = self._build_tool_result_message(call, tool_result)

            await session.append(result_message)
            state.record(result_message)
            await self._emit(
                Event(
                    kind="tool.call.end",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={
                        "call_id": call.call_id,
                        "tool_name": call.tool_name,
                        "ok": tool_err is None and (tool_result is None or tool_result.ok),
                    },
                )
            )
            await self._run_lifecycle_after_tool(state, call, result_message)

    async def _safe_tool_execute(
        self,
        tool: Tool,
        call: ToolCall,
        ctx: ToolContext,
    ) -> tuple[ToolResult | None, ToolError | None]:
        """执行工具并把异常包成 ToolError。绝不让工具异常穿透主循环。"""
        try:
            result = await tool.execute(dict(call.arguments), ctx)
            return result, None
        except AgentError as exc:
            # 保留具体 AgentError 语义（例如 ApprovalRejected 从 tool 层意外冒出来）
            if isinstance(exc, ToolError):
                return None, exc
            return None, ToolError(
                f"tool {call.tool_name!r} raised {type(exc).__name__}: {exc.message}",
                details={"call_id": call.call_id, "cause": type(exc).__name__},
            )
        except Exception as exc:
            return None, ToolError(
                f"tool {call.tool_name!r} raised {type(exc).__name__}: {exc}",
                details={"call_id": call.call_id, "exception_type": type(exc).__name__},
            )

    # ------------------------------------------------------------------
    # 消息构造助手
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tool_result_message(call: ToolCall, result: ToolResult) -> Message:
        metadata: dict[str, Any] = {"ok": result.ok}
        if result.data is not None:
            metadata["data"] = result.data
        if result.error_message:
            metadata["error_message"] = result.error_message
        return Message.tool_result(
            tool_call_id=call.call_id,
            content=result.content,
            name=call.tool_name,
            metadata=metadata,
        )

    @staticmethod
    def _build_tool_error_message(
        call: ToolCall,
        error_text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        meta = {"ok": False, "error_message": error_text}
        if metadata:
            meta.update(metadata)
        # 用 JSON 字符串承载错误文本，给下游 provider / 模型一个明确的结构化信号。
        content = json.dumps({"error": error_text}, ensure_ascii=False)
        return Message.tool_result(
            tool_call_id=call.call_id,
            content=content,
            name=call.tool_name,
            metadata=meta,
        )

    # ------------------------------------------------------------------
    # Lifecycle hook 调用（异常吞到 trace，不污染主链路）
    # ------------------------------------------------------------------

    async def _run_lifecycle_before_turn(self, state: RunState) -> None:
        for hook in self._lifecycle_hooks:
            before = getattr(hook, "before_turn", None)
            if before is None:
                continue
            try:
                await before(state)
            except Exception as exc:  # pragma: no cover - 防御式
                await self._emit_hook_error("before_turn", exc, state)

    async def _run_lifecycle_after_turn(self, state: RunState, message: Message) -> None:
        for hook in self._lifecycle_hooks:
            after = getattr(hook, "after_turn", None)
            if after is None:
                continue
            try:
                await after(state, message)
            except Exception as exc:  # pragma: no cover - 防御式
                await self._emit_hook_error("after_turn", exc, state)

    async def _run_lifecycle_before_tool(self, state: RunState, call: ToolCall) -> None:
        for hook in self._lifecycle_hooks:
            before = getattr(hook, "before_tool", None)
            if before is None:
                continue
            try:
                await before(state, call)
            except Exception as exc:  # pragma: no cover - 防御式
                await self._emit_hook_error("before_tool", exc, state)

    async def _run_lifecycle_after_tool(
        self, state: RunState, call: ToolCall, result_message: Message
    ) -> None:
        for hook in self._lifecycle_hooks:
            after = getattr(hook, "after_tool", None)
            if after is None:
                continue
            try:
                await after(state, call, result_message)
            except Exception as exc:  # pragma: no cover - 防御式
                await self._emit_hook_error("after_tool", exc, state)

    async def _emit_hook_error(self, phase: str, exc: Exception, state: RunState) -> None:
        await self._emit(
            Event(
                kind="error",
                run_id=state.run_id,
                turn=state.turn,
                payload={
                    "source": "lifecycle_hook",
                    "phase": phase,
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        )

    # ------------------------------------------------------------------
    # EventSink fan-out
    # ------------------------------------------------------------------

    async def _emit(self, event: Event) -> None:
        """把事件 fan-out 到所有 sink。sink 异常被吞掉以免污染主链路。"""
        for sink in self._event_sinks:
            try:
                await sink.emit(event)
            except Exception:
                # 观测层不允许影响主链路；这里静默忽略，
                # 未来可以把"sink 自己的错误"落到降级日志。
                continue

    # ------------------------------------------------------------------
    # 供装配层动态扩展 sink（可选）
    # ------------------------------------------------------------------

    def add_event_sink(self, sink: EventSink) -> None:
        """动态追加 event sink，便于装配层组合观测能力。"""
        self._event_sinks.append(sink)

    def add_lifecycle_hook(self, hook: LifecycleHook) -> None:
        """动态追加 lifecycle hook。"""
        self._lifecycle_hooks.append(hook)


__all__ = ["Runner"]
