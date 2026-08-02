"""Core 内部生命周期 hook。

``LifecycleHook`` 是 runner 内部的可选扩展协议。装配层可以注册
before_turn / after_turn / before_tool / after_tool / after_run hook，用于
调试、审计、dry-run 拦截和 run 结束后的业务扩展。

观测事件（Event）走 :class:`core.contracts.EventSink` fan-out，负责事实落地。
``LifecycleHook`` 由 Runner 在明确的机制点调用，负责业务扩展。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from core.contracts import Session
from core.message import Message, ToolCall
from core.result import Result
from core.run_state import RunState


@dataclass(frozen=True)
class LifecycleHookPointSpec:
    """一个命名 lifecycle 触发点的说明。"""

    name: str
    method_name: str
    timing: str
    payload: str
    examples: str


LIFECYCLE_HOOK_POINTS: tuple[LifecycleHookPointSpec, ...] = (
    # before_turn:
    # 已发生：run 已启动，本轮 turn_index 已递增，turn.start 事件已发出。
    # 未发生：session.history() 尚未读取，prompt 尚未组装，history compact 尚未执行，
    #        llm.request 尚未构造，模型调用尚未发生。
    # 适合：记录每轮开始审计、初始化/刷新 RunState.metadata 中的本轮标记、
    #      做 turn 级预算/节流检查、给后续 hook 写入轻量上下文。
    LifecycleHookPointSpec(
        name="before_turn",
        method_name="before_turn",
        timing="每轮 LLM 请求组装前，RunState.turn 已指向当前 turn",
        payload="state: RunState",
        examples="turn 开始审计；turn 级预算/节流检查；初始化 RunState.metadata 本轮标记",
    ),
    # after_turn:
    # 已发生：LLM 已返回，assistant message 已写入 session，RunState 已记录该 message，
    #        llm.response 和 usage 事件已发出。
    # 未发生：turn.end 尚未发出，assistant 里的 tool_calls 尚未进入工具执行循环，
    #        无 tool_call 时的 terminal Result 尚未构造。
    # 适合：观察模型计划、统计 assistant 输出、识别 tool_call 计划、为工具阶段准备审计上下文。
    LifecycleHookPointSpec(
        name="after_turn",
        method_name="after_turn",
        timing="assistant message 追加到 session 后，tool 调用执行前",
        payload="state: RunState, assistant_message: Message",
        examples="assistant 输出审计；tool_call 计划统计；workflow 进度同步；模型行为指标采集",
    ),
    # before_tool:
    # 已发生：assistant message 已产生 tool_call，Runner 正在处理单个 call。
    # 未发生：tool.call.start 尚未发出，工具名尚未查表失败，approval 尚未请求，
    #        tool.execute 尚未调用，tool_result 尚未构造。
    # 适合：记录原始 tool_call、做 scoped permission 审计准备、给审批/执行阶段打标签、
    #      统计工具调用意图。
    LifecycleHookPointSpec(
        name="before_tool",
        method_name="before_tool",
        timing="单个 tool call 审批和执行前",
        payload="state: RunState, call: ToolCall",
        examples="tool_call 意图审计；权限审计预记录；工具调用指标计数；为 after_tool 缓存 call 上下文",
    ),
    # after_tool:
    # 已发生：工具查表/审批/执行路径已完成，tool result message 已构造并写入 session，
    #        RunState 已记录该 result，tool.call.end 事件已发出。
    # 未发生：本 assistant message 的后续 tool_call 可能尚未处理完，下一轮 LLM 请求尚未开始。
    # 适合：检查工具执行结果、补充审计日志、同步 workflow/task 进度、统计工具成功/失败、
    #      触发与单个工具结果相关的旁路处理。
    LifecycleHookPointSpec(
        name="after_tool",
        method_name="after_tool",
        timing="单个 tool result message 构造并追加到 session 后，下一步 tool 或下一轮 LLM 前",
        payload="state: RunState, call: ToolCall, result_message: Message",
        examples="工具结果审计；失败原因归档；workflow/task 进度同步；工具成功率指标采集",
    ),
    # after_run:
    # 已发生：本次 run 已完成 completed/failed/cancelled 收口，Result 已构造，
    #        run.cancelled 或 error 等终态前置事件已按实际路径发出。
    # 未发生：run.end 尚未发出，runtime.aclose 尚未进入，宿主还未收到最终 Result 后续处理。
    # 适合：自我进化 after-run evidence window、run 级审计汇总、异步后台 review 调度、
    #      最终状态指标、跨 run 状态更新。
    LifecycleHookPointSpec(
        name="after_run",
        method_name="after_run",
        timing="Result 已构造，run.end 事件发出前；completed / failed / cancelled 都触发",
        payload="state: RunState, session: Session, result: Result",
        examples="自我进化 evidence window；run 级审计汇总；后台 review 调度；最终状态指标采集",
    ),
)
"""Runner 支持的 lifecycle 触发点清单。新增触发点先更新这里。"""


@runtime_checkable
class LifecycleHook(Protocol):
    """可选的生命周期 hook。

    runner 会在关键节点顺序调用所有注册的 hook，任何 hook 抛出的异常都应该
    被 runner 捕获并以 :class:`core.errors.AgentError` 的方式走错误路径，
    不允许让 hook 的异常穿透主链路。

    所有方法都是 async；默认什么都不做（通过可选 Protocol 语义，实现方可以只实现感兴趣的那一两个）。
    """

    async def before_turn(self, state: RunState) -> None: ...
    async def after_turn(self, state: RunState, assistant_message: Message) -> None: ...
    async def before_tool(self, state: RunState, call: ToolCall) -> None: ...
    async def after_tool(
        self, state: RunState, call: ToolCall, result_message: Message
    ) -> None: ...
    async def after_run(self, state: RunState, session: Session, result: Result) -> None: ...


class LifecycleHookBase:
    """默认空实现，业务 hook 只覆写自己关心的时机。"""

    async def before_turn(self, state: RunState) -> None:
        return None

    async def after_turn(self, state: RunState, assistant_message: Message) -> None:
        return None

    async def before_tool(self, state: RunState, call: ToolCall) -> None:
        return None

    async def after_tool(self, state: RunState, call: ToolCall, result_message: Message) -> None:
        return None

    async def after_run(self, state: RunState, session: Session, result: Result) -> None:
        return None


__all__ = [
    "LIFECYCLE_HOOK_POINTS",
    "LifecycleHook",
    "LifecycleHookBase",
    "LifecycleHookPointSpec",
]
