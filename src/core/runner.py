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

import asyncio
import json
import time
from collections.abc import Iterable, Sequence
from typing import Any

from core.agent_spec import AgentSpec
from core.contracts import (
    ApprovalProvider,
    ApprovalRequest,
    Event,
    EventSink,
    FinishReason,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    MessageCompactor,
    PromptAssembler,
    PromptDebugSink,
    PromptSource,
    Session,
    SupportsLLMStream,
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
        input_assembler: PromptAssembler | None = None,
        instruction_sources: Sequence[PromptSource] | None = None,
        prompt_debug_sink: PromptDebugSink | None = None,
        instruction_origins: Sequence[str] | None = None,
        stream_enabled: bool = False,
        suppress_content_after_tool_call: bool = True,
    ) -> None:
        self._event_sinks: list[EventSink] = list(event_sinks or [])
        self._lifecycle_hooks: list[LifecycleHook] = list(lifecycle_hooks or [])
        # 可选：把 history 送给 LLM 之前过一道 compactor；None 表示原样透传。
        self._message_compactor: MessageCompactor | None = message_compactor
        # 可选：PromptAssembler 接管 prompt build（system 注入 + compact）。
        # None 时退化到旧路径（_prepare_messages + _seed_messages 的 system 注入）。
        self._input_assembler: PromptAssembler | None = input_assembler
        # 静态指令来源列表，随 assembler 一起传入；None 时等价于空序列。
        self._instruction_sources: Sequence[PromptSource] = list(instruction_sources or [])
        self._prompt_debug_sink: PromptDebugSink | None = prompt_debug_sink
        self._instruction_origins: list[str] = list(instruction_origins or [])
        # 流式开关（v0.2 接入阶段）。装配层从 cfg.stream.enabled 注入；
        # provider 须满足 SupportsLLMStream Protocol 才会实际走流式（runner 用
        # isinstance 探测，不靠 NotImplementedError 控制流）。
        self._stream_enabled: bool = stream_enabled
        # 流中出现 tool_call 后，是否屏蔽继续到达的 content.delta（避免 CLI 在
        # 工具调用前打印夹带的乱文本）。仅影响事件 emit；message.done 中的
        # content 不受影响（runner 只在事件层做屏蔽，session 仍记录完整 message）。
        self._suppress_content_after_tool_call: bool = suppress_content_after_tool_call

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
        attachments: list[dict[str, Any]] | None = None,
        lifecycle_hooks: Sequence[LifecycleHook] | None = None,
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
            attachments: 用户输入附件 ref 列表（``UserInputAttachment.model_dump()``
                输出的 dict 形态）。非 None 时会写到首条 user :class:`Message`
                的 ``metadata["attachments"]``，供 InputAssembler / provider
                组装多模态输入。CLI 路径默认 None，不影响纯文本对话。
                详见 ``dev-pipeline/tasks/claude-image-paste-e2e/README.md`` §1 / §4。
            lifecycle_hooks: 本次 run 的临时 lifecycle hook，只作用于当前 run。

        Returns:
            :class:`Result`：运行结束的统一结果。

        Raises:
            不直接抛异常；所有异常都收口到 :class:`Result`：

            - :class:`AgentError` / 意外 :class:`Exception` → ``status="failed"``，
              ``error`` 字段承载具体异常
            - :class:`asyncio.CancelledError` → ``status="cancelled"``（v0.1
              interrupt-run-v0.1 起），不向外 re-raise；上游（SessionBridge /
              CommandService / Web ws.py）只需要按 ``Result.status`` 分支即可，
              不需要自己 ``except CancelledError``。

            这是为了让 host / cli 只关心 Result 即可。
        """

        # run_id 不再在入口生成 uuid；改为 _seed_messages 内 advance_run_index 后拼装
        # (`run-{session_id}-{n}`)。外部传入的 run_id 仍然兜底优先（测试注入等场景）。
        # state.run_id 起始为 "" 占位，_seed_messages 写真值后再 emit run.start。
        run_id = run_id or ""
        effective_max_turns = max_turns if max_turns is not None else agent_spec.max_turns
        effective_lifecycle_hooks = [
            *self._lifecycle_hooks,
            *(lifecycle_hooks or ()),
        ]

        state = RunState(run_id=run_id, session_id=session.session_id)
        state.mark_running()

        try:
            resolved_tools = self._resolve_tools(agent_spec, tools, enabled_tools)
            # _seed_messages 内部完成 user message append + advance_run_index +
            # 写 state.run_id；之后所有 emit / Result 都读 state.run_id（已落定）。
            await self._seed_messages(
                session, agent_spec, user_input, state, attachments=attachments
            )
            await self._emit(
                Event(
                    kind="run.start",
                    run_id=state.run_id,
                    payload={
                        "session_id": session.session_id,
                        "agent": agent_spec.name,
                        "model": agent_spec.default_model,
                        "max_turns": effective_max_turns,
                        "reasoning_effort": agent_spec.reasoning_effort,
                    },
                )
            )
            final_message, accumulated_usage = await self._drive_turns(
                state=state,
                session=session,
                agent_spec=agent_spec,
                llm=llm,
                tools_by_name={t.name: t for t in resolved_tools},
                resolved_tools=resolved_tools,
                approval=approval,
                max_turns=effective_max_turns,
                lifecycle_hooks=effective_lifecycle_hooks,
            )
            state.mark_completed()
            result = Result(
                run_id=state.run_id,
                session_id=session.session_id,
                status="completed",
                final_message=final_message,
                turn_count=state.turn,
                metadata={"usage": accumulated_usage} if accumulated_usage else {},
            )
        except asyncio.CancelledError:
            # interrupt-run-v0.1：外部 task.cancel()（典型 = 用户点 Stop）。
            # 不向外 re-raise，统一收口到 Result(status="cancelled")，与
            # AgentError / Exception 分支语义对齐（runner 不抛，host 只看 Result）。
            #
            # 此前 _execute_tool_calls 已对当前未配对 tool_use 合成占位 tool_result，
            # 保证 session jsonl 里 tool_use ↔ tool_result 一一对应（Anthropic 协议
            # 要求）；这里只负责状态机收尾 + emit 事件。
            state.mark_cancelled()
            # state.metadata 是 dict[str, str]；空字符串视为"未在 tool 阶段"。
            _cancelled_id_raw = state.metadata.get("cancelled_tool_call_id", "")
            cancelled_tool_call_id: str | None = _cancelled_id_raw if _cancelled_id_raw else None
            cancel_meta: dict[str, object] = {
                "cancelled_at_turn": state.turn,
                "cancelled_tool_call_id": cancelled_tool_call_id,
                "cancel_reason": "user_interrupt",
            }
            await self._emit(
                Event(
                    kind="run.cancelled",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload=cancel_meta,
                )
            )
            result = Result(
                run_id=state.run_id,
                session_id=session.session_id,
                status="cancelled",
                final_message=None,
                turn_count=state.turn,
                error=None,
                metadata=cancel_meta,
            )
        except AgentError as exc:
            state.mark_failed(exc)
            await self._emit(
                Event(
                    kind="error",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={"type": type(exc).__name__, "message": exc.message},
                )
            )
            result = Result(
                run_id=state.run_id,
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
                    run_id=state.run_id,
                    turn=state.turn,
                    payload={"type": "UnexpectedError", "message": str(exc)},
                )
            )
            result = Result(
                run_id=state.run_id,
                session_id=session.session_id,
                status="failed",
                final_message=None,
                turn_count=state.turn,
                error=wrapped,
            )

        await self._emit(
            Event(
                kind="run.end",
                run_id=state.run_id,
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
        *,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """如果 session 里还没有 system 指令，就先把 AgentSpec.instructions 注入。

        当 ``input_assembler`` 已注入时，system 注入职责移交给 assembler，
        这里只写 user 首条消息，避免双重 system 注入。

        本方法承担两件事：
        1. user message append（必做）
        2. ``session.advance_run_index()`` 拼装 ``state.run_id``（仅当外部未注入 run_id 时）

        ``attachments`` 非 None 时透传到 ``Message.metadata["attachments"]``；
        全链路保持 dict 形态（``UserInputAttachment.model_dump()`` 输出），
        避免 provider / assembler 还要处理"原始 BaseModel 还是 dict"两种形态。
        """
        if self._input_assembler is None:
            # 旧路径：由 _seed_messages 自己写 system 消息（兼容 fallback）。
            existing = await session.history()
            has_system = any(m.role == "system" for m in existing)
            if not has_system and agent_spec.instructions:
                system_msg = Message.system(agent_spec.instructions)
                await session.append(system_msg)
                state.record(system_msg)
        # 始终写入 user 消息。attachments 非 None 时写到 metadata。
        user_metadata: dict[str, Any] | None = {"attachments": attachments} if attachments else None
        user_msg = Message.user(user_input, metadata=user_metadata)
        await session.append(user_msg)
        # 紧跟 user message append 调 advance_run_index，把"用户消息入历史"和
        # "run 编号递增"绑到同一时机。
        #
        # 非事务原子约定（v0.x 简化）：append + advance 分两步执行，不构成单事务。
        # 失败模式：
        #   - append 成功 + advance 失败 → 下次启动 manifest run_count 不变，
        #     新一轮 advance 拿到的 run_index 复用上次值；但本 run 已崩溃未生成
        #     run_id 持久记录，新一轮 run_id 仍唯一不撞，不丢消息也不重号。
        #   - advance 成功 + append 失败（极罕见） → jsonl 少一条，run_count 跳号，
        #     仍唯一不撞。
        # 不会丢消息也不让 run_id 重复。事务化留 v0.2+。
        #
        # 外部注入 run_id（state.run_id 非空，多见于测试 fixture）时跳过 advance，
        # 保持外部传入的标识不变。
        if not state.run_id:
            run_index = await session.advance_run_index()
            state.run_id = f"run-{session.session_id}-{run_index}"
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
        lifecycle_hooks: Sequence[LifecycleHook],
    ) -> tuple[Message | None, dict[str, Any]]:
        """核心 turn 循环。返回 (最终 assistant 消息, 累计 usage)。"""
        final_assistant: Message | None = None
        accumulated_usage: dict[str, int] = {}

        while True:
            if state.turn >= max_turns:
                raise MaxTurnsExceededError(
                    f"exceeded max_turns={max_turns} without reaching a terminal response",
                    details={"run_id": state.run_id, "max_turns": max_turns},
                )

            state.advance_turn()
            await self._emit(Event(kind="turn.start", run_id=state.run_id, turn=state.turn))
            await self._run_lifecycle_before_turn(state, lifecycle_hooks)

            history = await session.history()

            if self._input_assembler is not None:
                # 新路径：InputAssembler 接管 compact + system 注入。
                assembled = await self._input_assembler.assemble(
                    history,
                    self._instruction_sources,
                )
                prepared_messages = assembled.messages
                if self._prompt_debug_sink is not None:
                    system_message = getattr(assembled, "system_message", None)
                    self._prompt_debug_sink.dump(
                        session_id=session.session_id,
                        run_id=state.run_id,
                        turn=state.turn,
                        model=agent_spec.default_model,
                        instruction_origins=self._instruction_origins,
                        history_before_assemble=history,
                        assembled_messages=prepared_messages,
                        metadata=assembled.metadata,
                        added_system_prompt=(
                            system_message.content if system_message is not None else None
                        ),
                    )
                # 把 assembler metadata 映射成 compact_meta 格式（若有压缩）。
                original_count = assembled.metadata.get("original_count", len(history))
                compacted_count = assembled.metadata.get("compacted_count", len(prepared_messages))
                if compacted_count != original_count:
                    compact_meta: dict[str, Any] | None = {
                        "original_count": original_count,
                        "compacted_count": compacted_count,
                        "dropped_count": original_count - compacted_count,
                    }
                else:
                    compact_meta = None
            else:
                # 旧路径：_prepare_messages 做 compact，system 已由 _seed_messages 注入。
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
            # claude-image-paste-e2e §5：把 ``session_id``(Web 路径 == ``thread_id``,
            # 见 src/web/thread_metadata.py:112)透传到 provider,供
            # :class:`executors.llm.anthropic_messages.AnthropicMessagesProvider`
            # 还原附件物理路径(``.kongming/web/uploads/images/<thread_id>/<asset_id>.<ext>``)。
            # CLI 路径无 attachments,thread_id 取值不命中 storage,无副作用。
            llm_request = LLMRequest(
                model=agent_spec.default_model,
                messages=tuple(prepared_messages),
                tools=tuple(resolved_tools),
                metadata={"thread_id": session.session_id},
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

            if self._stream_enabled and isinstance(llm, SupportsLLMStream):
                response = await self._consume_stream(
                    llm, llm_request, run_id=state.run_id, turn=state.turn
                )
            else:
                response = await self._safe_llm_complete(llm, llm_request)
                # 非流式路径：把完整内容包装成一次 content.delta 发出，
                # 让下游（WS EventSink → 前端流式渲染）能收到内容。
                # 流式路径由 _consume_stream 逐 chunk 发，不需要这里补。
                if response.message.content:
                    await self._emit(
                        Event(
                            kind="content.delta",
                            run_id=state.run_id,
                            turn=state.turn,
                            payload={"delta": response.message.content, "seq": 0},
                        )
                    )
            assistant_message = response.message
            assistant_usage = dict(response.usage) if response.usage else None
            await session.append(assistant_message, usage=assistant_usage)
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
                        "provider_metadata": dict(response.provider_metadata),
                    },
                )
            )

            # 独立 usage event，供 WS EventSink 推送到前端 StatusLine
            # task#3.1：payload 透传 LLMResponse.usage 全字段（含 provider_kind +
            # SDK 原生字段），让 web.usage_token.UsagePersistSink 按 channel 解析；
            # 同时保留 prompt/completion/total 兼容老消费者。
            u = response.usage
            await self._emit(
                Event(
                    kind="usage",
                    run_id=state.run_id,
                    turn=state.turn,
                    payload=dict(u),
                )
            )

            # 跨 turn 累计 usage，最终写入 Result.metadata。
            for key, val in response.usage.items():
                if isinstance(val, int):
                    accumulated_usage[key] = accumulated_usage.get(key, 0) + val

            await self._run_lifecycle_after_turn(state, assistant_message, lifecycle_hooks)
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
                lifecycle_hooks=lifecycle_hooks,
            )
            # 回填后继续下一个 turn

        return final_assistant, dict(accumulated_usage)

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

    async def _consume_stream(
        self,
        llm: SupportsLLMStream,
        request: LLMRequest,
        *,
        run_id: str,
        turn: int,
    ) -> LLMResponse:
        """消费 :class:`SupportsLLMStream` 的 chunk 流，返回等价 :class:`LLMResponse`。

        与 :meth:`_safe_llm_complete` 行为等价：上层（``_drive_turns``）在拿到
        ``LLMResponse`` 之后的 tool_call / approval / session.append 流程完全不
        受流式/非流式影响。

        中间会向 ``EventSink`` 发射四类事件：

        - ``llm.chunk.first``：首个非空 chunk 抵达时 emit 一次（用于 TTFT 度量）
        - ``content.delta`` / ``reasoning.delta``：每个增量 emit
        - ``llm.stream.end``：流终止时（成功或异常）汇总 emit 一次

        其它语义：

        - ``tool_call.*`` chunk 在 runner 内累积，**不**直接 emit 为 EventSink 事件
          （CLI 不渲染中间状态；最终的 ``ToolCall`` 来自 ``message.done``）
        - 出现 ``tool_call.start`` 后若 ``suppress_content_after_tool_call=True``，
          后续 ``content.delta`` 不再 emit；但 session 仍保留完整 message（来自
          ``message.done``）
        - 流非正常结束（无 ``message.done``）→ 抛 :class:`ProviderError`
        """
        started_ns = time.monotonic_ns()
        first_chunk_emitted = False
        tool_call_seen = False
        chunk_count = 0
        content_chars = 0
        reasoning_chars = 0
        tool_call_count = 0
        truncated_args = False

        final_message: Message | None = None
        final_finish_reason: FinishReason = "other"
        final_usage: dict[str, Any] = {}
        final_provider_metadata: dict[str, Any] = {}

        try:
            async for chunk in llm.stream(request):
                chunk_count += 1

                # TTFT：首个非空 chunk 抵达时 emit 一次（不在 message.done 上触发，
                # 因为 message.done 是终态汇总，不算"首个内容到达"）
                if not first_chunk_emitted and chunk.kind != "message.done":
                    elapsed_ms = (time.monotonic_ns() - started_ns) // 1_000_000
                    await self._emit(
                        Event(
                            kind="llm.chunk.first",
                            run_id=run_id,
                            turn=turn,
                            payload={"elapsed_ms": elapsed_ms, "model": request.model},
                        )
                    )
                    first_chunk_emitted = True

                kind = chunk.kind
                if kind == "content.delta":
                    content_chars += len(chunk.delta)
                    if tool_call_seen and self._suppress_content_after_tool_call:
                        # suppression：tool_call 后的 content 不 emit
                        continue
                    await self._emit(
                        Event(
                            kind="content.delta",
                            run_id=run_id,
                            turn=turn,
                            payload={"delta": chunk.delta, "index": chunk.index},
                        )
                    )
                elif kind == "reasoning.delta":
                    reasoning_chars += len(chunk.delta)
                    await self._emit(
                        Event(
                            kind="reasoning.delta",
                            run_id=run_id,
                            turn=turn,
                            payload={"delta": chunk.delta},
                        )
                    )
                elif kind == "tool_call.start":
                    tool_call_seen = True
                    tool_call_count += 1
                    # 不 emit（runner 内部累积；最终 ToolCall 由 message.done 提供）
                elif kind in ("tool_call.arguments.delta", "tool_call.end"):
                    # 内部累积；不 emit
                    pass
                elif kind == "message.done":
                    if chunk.message is None:
                        raise ProviderError(
                            "stream message.done chunk missing message",
                            details={"run_id": run_id, "turn": turn},
                        )
                    final_message = chunk.message
                    final_finish_reason = chunk.finish_reason or "other"
                    final_usage = dict(chunk.usage)
                    final_provider_metadata = dict(chunk.provider_metadata)
                    # parser 把 JSON 截断降级为 length；此处记一下供 stream.end payload
                    if final_finish_reason == "length":
                        truncated_args = True
                # 其它未知 kind：忽略，不破坏主链路

            if final_message is None:
                raise ProviderError(
                    "stream ended without message.done chunk",
                    details={"run_id": run_id, "turn": turn, "chunk_count": chunk_count},
                )

        except AgentError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"LLM stream consumption failed: {exc!r}",
                details={
                    "exception_type": type(exc).__name__,
                    "run_id": run_id,
                    "turn": turn,
                },
            ) from exc
        finally:
            # 终态汇总（流终止 = 成功 or 异常都 emit 一次）
            await self._emit(
                Event(
                    kind="llm.stream.end",
                    run_id=run_id,
                    turn=turn,
                    payload={
                        "chunk_count": chunk_count,
                        "finish_reason": (
                            final_finish_reason if final_message is not None else "error"
                        ),
                        "content_chars": content_chars,
                        "reasoning_chars": reasoning_chars,
                        "tool_call_count": tool_call_count,
                        "truncated_args": truncated_args,
                    },
                )
            )

        return LLMResponse(
            message=final_message,
            finish_reason=final_finish_reason,
            usage=final_usage,
            provider_metadata=final_provider_metadata,
        )

    async def _execute_tool_calls(
        self,
        *,
        state: RunState,
        session: Session,
        approval: ApprovalProvider,
        tool_calls: Iterable[ToolCall],
        tools_by_name: dict[str, Tool],
        lifecycle_hooks: Sequence[LifecycleHook],
    ) -> None:
        """串行执行 assistant 消息携带的所有 tool_call，把结果回填到 session。

        **interrupt-run-v0.1**：每个 call 用单独 try 包裹，捕获
        :class:`asyncio.CancelledError` 后：

        1. 给当前正在跑的 call 写一条 ``[interrupted]`` tool_result 占位
           （``is_error=True``、``interrupted=True`` metadata）；
        2. 给同一 assistant 消息里**尚未起跑**的剩余 call 也补占位 —— Anthropic
           协议要求 "同一条 assistant 消息所有 tool_use 必须对应 tool_result"，
           少一条下次 LLM 调用会被服务端 400 拒掉；
        3. 把当前被打断的 call_id 写入 ``state.metadata["cancelled_tool_call_id"]``
           供 runner 顶层 except 读出来塞进 Result.metadata；
        4. 重新 raise 让 runner 顶层接 → ``Result(status="cancelled")``。

        其它异常（AgentError / 普通 Exception）由 ``_safe_tool_execute`` 包成
        :class:`ToolError`，不在本方法 except 范围；走原 ``tool_err`` 分支。
        """
        # 物化为 list：cancel 时需要知道 "剩余多少个 call 没起跑"。
        calls_list = list(tool_calls)
        for idx, call in enumerate(calls_list):
            try:
                await self._execute_single_tool_call(
                    state=state,
                    session=session,
                    approval=approval,
                    call=call,
                    tools_by_name=tools_by_name,
                    lifecycle_hooks=lifecycle_hooks,
                )
            except asyncio.CancelledError:
                # 1. 当前 call 占位（覆盖本 call 的任何中间 await 被打断的情形）
                await self._finalize_unpaired_call(
                    state=state,
                    session=session,
                    call=call,
                    reason="user_interrupt",
                )
                state.metadata["cancelled_tool_call_id"] = call.call_id
                # 2. 剩余未起跑的 call 也占位
                for remaining in calls_list[idx + 1 :]:
                    await self._finalize_unpaired_call(
                        state=state,
                        session=session,
                        call=remaining,
                        reason="user_interrupt_pending",
                    )
                # 3. 透传给 runner 顶层 except CancelledError
                raise

    async def _execute_single_tool_call(
        self,
        *,
        state: RunState,
        session: Session,
        approval: ApprovalProvider,
        call: ToolCall,
        tools_by_name: dict[str, Tool],
        lifecycle_hooks: Sequence[LifecycleHook],
    ) -> None:
        """执行单个 tool_call。从原 ``_execute_tool_calls`` 循环体抽出。

        把"单 call 全流程"独立成方法的目的：让外层 for 循环只需 try 一次就能
        统一处理 cancel；同时 lifecycle hook / approval / 执行 / 回填 的顺序
        与原版完全一致，没有行为变化。
        """
        await self._run_lifecycle_before_tool(state, call, lifecycle_hooks)
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
            error_message = f"tool {call.tool_name!r} not registered"
            result_message = self._build_tool_error_message(call, error_message)
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
                        "content": "",
                        "data": None,
                        "error_message": error_message,
                    },
                )
            )
            await self._run_lifecycle_after_tool(state, call, result_message, lifecycle_hooks)
            return

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
            error_message = f"approval {decision.outcome}: {reason}"
            rejected_msg = self._build_tool_error_message(
                call,
                error_message,
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
                        "content": "",
                        "data": None,
                        "error_message": error_message,
                    },
                )
            )
            await self._run_lifecycle_after_tool(state, call, rejected_msg, lifecycle_hooks)
            # 审批拒绝不直接终止 run；把"被拒绝"这条事实喂回模型，
            # 由模型决定下一步。这样 safety 的策略层可以通过文本说明 / reason
            # 让模型调整计划，而不需要立刻结束 run。
            return

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
        # tool.call.end payload 携带 ToolResult 4 字段（content / data / ok /
        # error_message），让下游 sink（trace / web）拿到工具产出。
        # tool_err 路径走异常分支，content="" + error_message=异常消息。
        if tool_err is not None:
            payload_content = ""
            payload_data: dict[str, Any] | None = None
            payload_error_message: str | None = tool_err.message
        else:
            assert tool_result is not None
            payload_content = tool_result.content
            payload_data = tool_result.data
            payload_error_message = tool_result.error_message
        await self._emit(
            Event(
                kind="tool.call.end",
                run_id=state.run_id,
                turn=state.turn,
                payload={
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                    "ok": tool_err is None and (tool_result is None or tool_result.ok),
                    "content": payload_content,
                    "data": payload_data,
                    "error_message": payload_error_message,
                },
            )
        )
        await self._run_lifecycle_after_tool(state, call, result_message, lifecycle_hooks)

    async def _finalize_unpaired_call(
        self,
        *,
        state: RunState,
        session: Session,
        call: ToolCall,
        reason: str,
    ) -> None:
        """给一个未配对的 tool_use 写占位 tool_result + emit tool.call.end。

        触发场景（interrupt-run-v0.1）：

        - ``reason="user_interrupt"``：当前正在跑的 call 被 cancel
        - ``reason="user_interrupt_pending"``：同 assistant 消息里**未起跑**的
          剩余 call —— Anthropic / OpenAI 协议要求所有 tool_use 必须有对应
          tool_result，不补占位下次 LLM 调用会被服务端 400 拒掉

        占位消息 metadata：``{"ok": False, "interrupted": True,
        "interrupt_reason": reason, "error_message": "..."}``，content 为
        JSON 字符串 ``{"error": "[interrupted by user: ...]"}``，
        :meth:`_build_tool_error_message` 复用，与 approval 拒绝等 error 路径
        一致，HistoryCompactor 不会把它过滤掉（``role=="tool"`` 无条件保留）。
        """
        error_text = f"[interrupted by user: {reason}]"
        result_message = self._build_tool_error_message(
            call,
            error_text,
            metadata={"interrupted": True, "interrupt_reason": reason},
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
                    "reason": "interrupted",
                    "content": "",
                    "data": None,
                    "error_message": error_text,
                },
            )
        )

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

    async def _run_lifecycle_before_turn(
        self,
        state: RunState,
        lifecycle_hooks: Sequence[LifecycleHook],
    ) -> None:
        for hook in lifecycle_hooks:
            before = getattr(hook, "before_turn", None)
            if before is None:
                continue
            try:
                await before(state)
            except Exception as exc:  # pragma: no cover - 防御式
                await self._emit_hook_error("before_turn", exc, state)

    async def _run_lifecycle_after_turn(
        self,
        state: RunState,
        message: Message,
        lifecycle_hooks: Sequence[LifecycleHook],
    ) -> None:
        for hook in lifecycle_hooks:
            after = getattr(hook, "after_turn", None)
            if after is None:
                continue
            try:
                await after(state, message)
            except Exception as exc:  # pragma: no cover - 防御式
                await self._emit_hook_error("after_turn", exc, state)

    async def _run_lifecycle_before_tool(
        self,
        state: RunState,
        call: ToolCall,
        lifecycle_hooks: Sequence[LifecycleHook],
    ) -> None:
        for hook in lifecycle_hooks:
            before = getattr(hook, "before_tool", None)
            if before is None:
                continue
            try:
                await before(state, call)
            except Exception as exc:  # pragma: no cover - 防御式
                await self._emit_hook_error("before_tool", exc, state)

    async def _run_lifecycle_after_tool(
        self,
        state: RunState,
        call: ToolCall,
        result_message: Message,
        lifecycle_hooks: Sequence[LifecycleHook],
    ) -> None:
        for hook in lifecycle_hooks:
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
