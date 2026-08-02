"""run 终态分类器（模块 D）。

把 runner 收口后的 :class:`Result`（status=completed/failed/cancelled + metadata）
翻译成 :class:`Disposition`（带 action + reason 的封闭数据），让 cancelled 从
「异常控制流」剥成「数据值」，让消费侧（agent_loop）按 action 二选一，不遍地 match。

设计要点（见 ``docs/spec/agent-tree-v0.1/04-data-and-state.md`` Disposition 定义 +
``docs/research/claude错误分类器.md`` Claude Code classify 家族范式）：

- **classify 产数据不产控制流**（对齐 Claude Code classifyAPIError/classifyToolError
  范式：classify 返回标签，消费方按 action 查表，不写 N 个 if/else）。
- :class:`Disposition` 是单 frozen dataclass（**不再 4 个子类**）：``action`` 决定
  「上投 vs 不上投」，``reason`` 是封闭枚举标签（completed / max_turns /
  user_interrupt / llm.auth / tool.execution / internal.bug ...）让父 agent / 遥测
  区分来源；``tree_wide`` 仅 cancel 类 reason 有意义。
- :func:`classify_result` 是纯函数：吃 ``Result`` + ``current_epoch``，返回
  ``Disposition``；无 IO、无副作用、永不抛异常（覆盖不到的组合兜底
  ``Disposition("deliver_up", "internal.bug")``）。
- :func:`build_mail` 是上投 Mail 构造助手（按 reason 决定 payload），被 task-4
  agent_loop / task-5 AgentManager 消费；本模块只 import core 自身类型。

边界内外分工：边界以内（provider/tool/runner）已用 try/finally 清理资源 + 翻译成
Result；本模块是 runner 之上的翻译层；边界以外只有 agent_loop 一个按 action 二选
一的消费点。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.errors import MaxTurnsExceededError, ProviderError, ToolError
from core.result import Result

if TYPE_CHECKING:
    # Mail 类型在 task-4（core.mail，模块 C · Mailbox）定义。这里只在类型检查期
    # 引用，运行时不 import task-4，避免 core → mailbox 反向依赖（import-linter
    # Contract 1）。core.mail 尚未由 task-4 提供（运行期不存在），mypy 将其当隐式
    # Any 处理不报错；task-4 合并 core.mail 后此 TYPE_CHECKING import 自动就绪。
    from core.mail import Mail
    from core.message import Message

# ---------------------------------------------------------------------------
# 封闭枚举（事实源：docs/spec/agent-tree-v0.1/04-data-and-state.md）
# ---------------------------------------------------------------------------

# run 终态的来源标签（封闭 Literal，mypy 可穷尽检查）。替代原 Outcome 4 子类的
# 异构字段（Completed 无字段 / Exhausted.budget / Cancelled.reason+tree_wide /
# Failed.error_class），统一成单一 tag 维度。
Reason = Literal[
    # —— 正常终态 ——
    "completed",  # run 走完全部 turn，正常返回
    "max_turns",  # 预算耗尽：turn 数上限
    "token_limit",  # 预算耗尽：token 上限
    "cost_limit",  # 预算耗尽：成本上限
    # —— 取消（控制面，非错误）——
    "user_interrupt",  # 用户点 Stop（典型树级）
    "parent_cascade",  # 父被砍连带自己（树级）
    "hook_blocked",  # lifecycle hook 拦截（通常局部）
    "watchdog",  # 看门狗超时（典型树级）
    # —— 失败（错误面）——
    "llm.capability",  # provider 能力/限制（model_error / blocking_limit / 5xx）
    "llm.protocol_400",  # 协议 400（prompt_too_long 等）
    "llm.auth",  # 鉴权失败（401/403）
    "llm.rate_limit",  # 限流（429）
    "tool.execution",  # 工具执行失败
    "internal.bug",  # 兜底：未知 status / None error / 未知异常类
]

# Disposition.action —— 消费侧二选一的唯一依据（agent_loop 不再写 N 个 match）。
Action = Literal["deliver_up", "emit_only"]


# ---------------------------------------------------------------------------
# Disposition —— run 终态分类结果（单 dataclass，不再 4 子类）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Disposition:
    """run 终态分类结果（run terminal-state classifier output）。

    classify 只产数据不产控制流；消费方（agent_loop）按 :attr:`action` 二选一：
    ``emit_only`` → 不上投只 emit（树级取消）；``deliver_up`` → 上投 / 推 UI
    （其余所有情况）。``reason`` 标签让父 agent / 遥测区分来源（completed vs
    failed vs 局部 cancel），不是用来决定分支的——分支只看 action。

    Attributes:
        action: 消费侧唯一分支依据。``emit_only`` = 树级取消不上投；
            ``deliver_up`` = 上投 / 推 UI。
        reason: 来源标签（封闭枚举 :data:`Reason`）。父 agent 拿到上投 Mail 的
            payload 时据此区分「子完成 / 子失败 / 子预算耗尽 / 子被局部取消」，
            遥测据此聚合。
        tree_wide: 仅 cancel 类 reason 有意义。True = 父也被砍（action=emit_only）；
            False = 局部取消（action=deliver_up，上投 cancelled notice）。
            非 cancel reason 时固定 False。
    """

    action: Action
    reason: Reason
    tree_wide: bool = False


# ---------------------------------------------------------------------------
# classify_result 纯函数
# ---------------------------------------------------------------------------

# tree_wide 推荐值静态表（classify 产出，仅 cancel 类 reason 用）。
# 事实源：README 技术设计「tree_wide 推荐值规则」。
# - user_interrupt → True：用户打断典型是树级 Stop。
# - hook_blocked   → False：hook 拦截通常是局部的，需上投通知父。
# - parent_cascade → True：父被砍连带自己，不上投。
# - watchdog       → True：看门狗超时通常是树级处置。
_TREE_WIDE_RECOMMENDATION: dict[str, bool] = {
    "user_interrupt": True,
    "hook_blocked": False,
    "parent_cascade": True,
    "watchdog": True,
}

# cancel_reason → Reason 映射；未在表内的 reason 兜底 user_interrupt。
# runner 现状（runner.py）写 "user_interrupt"；task-3/4 引入 parent_cascade /
# hook_blocked / watchdog 时复用同一 metadata["cancel_reason"] 键。
_CANCEL_REASON_MAP: dict[str, Reason] = {
    "user_interrupt": "user_interrupt",
    "parent_cascade": "parent_cascade",
    "hook_blocked": "hook_blocked",
    "watchdog": "watchdog",
}


def classify_result(result: Result, current_epoch: int) -> Disposition:
    """把 :class:`Result` 翻译成 :class:`Disposition`（run 终态分类）。

    纯函数：无 IO、无副作用、无对 mailbox/registry/ThreadCell 的依赖；
    永不抛异常，所有覆盖不到的组合兜底 ``Disposition("deliver_up", "internal.bug")``。

    action 映射规则（消费侧二选一的依据）：
    - ``emit_only``：cancel 类 reason 且 tree_wide=True（树级取消，父也被砍，不上投）
    - ``deliver_up``：其余所有情况（completed / exhausted / failed / 局部 cancel
      都上投，payload 里带 reason 让父 agent 区分来源）

    Args:
        result: runner 收口后的统一结果。
        current_epoch: agent_loop 启动参数（世代计数器）。仅用于辅助 cancelled 的
            tree_wide 推荐值记录（当前实现推荐值是按 reason 的静态表，本参数不
            改变推荐值）；不读全局状态，避免内核反向依赖装配层。

    Returns:
        :class:`Disposition`（action + reason + tree_wide）。
    """
    # 整个 classify 包一层兜底：即便出现意外异常也绝不向上抛，保证 agent_loop
    # 消费侧不因分类器自身 bug 而崩溃。
    try:
        # 按 str 比较，避免 mypy 基于 ResultStatus Literal 收窄后把兜底分支判为
        # unreachable —— 运行期可能通过 cast/反射写入非标准 status，兜底必须可达。
        status: str = result.status
    except Exception:
        return Disposition(action="deliver_up", reason="internal.bug")

    if status == "completed":
        return _classify_completed(result)
    if status == "cancelled":
        return _classify_cancelled(result, current_epoch)
    if status == "failed":
        return _classify_failed(result)
    # 理论不会出现（ResultStatus 只有 3 种），兜底 internal.bug。
    return Disposition(action="deliver_up", reason="internal.bug")


def _classify_completed(result: Result) -> Disposition:
    """status=completed：MaxTurnsExceededError → Exhausted(max_turns)，否则 Completed。

    action 都固定 ``deliver_up``（成功 / 预算耗尽都上投，payload 区分）。
    """
    if isinstance(result.error, MaxTurnsExceededError):
        return Disposition(action="deliver_up", reason="max_turns")
    return Disposition(action="deliver_up", reason="completed")


def _classify_cancelled(result: Result, current_epoch: int) -> Disposition:
    """status=cancelled：按 metadata["cancel_reason"] 映射 reason + tree_wide 推荐值。

    - 未知 / 缺失 / 空串 reason → 兜底 ``user_interrupt``（runner 现状默认值）。
    - tree_wide 取 ``_TREE_WIDE_RECOMMENDATION`` 静态表；mapped reason 不在表
      时兜底 True（user_interrupt 的推荐值）。
    - action：tree_wide=True → ``emit_only``（树级取消不上投）；
      tree_wide=False → ``deliver_up``（局部取消上投 cancelled notice）。
    - ``current_epoch`` 仅记录用，当前不改变推荐值；保留参数以对齐 agent_loop
      调用签名 + 为 task-4 覆盖逻辑留钩子。
    """
    metadata = result.metadata or {}
    raw_reason = metadata.get("cancel_reason", "")
    # metadata 是 dict[str, object]，取出的值是 object，需转 str 兜底。
    raw_reason_str = raw_reason if isinstance(raw_reason, str) else ""
    reason: Reason = _CANCEL_REASON_MAP.get(raw_reason_str, "user_interrupt")
    tree_wide = _TREE_WIDE_RECOMMENDATION.get(reason, True)
    action: Action = "emit_only" if tree_wide else "deliver_up"
    return Disposition(action=action, reason=reason, tree_wide=tree_wide)


def _classify_failed(result: Result) -> Disposition:
    """status=failed：按 type(result.error) 子类映射 reason。

    action 固定 ``deliver_up``（失败通知上投）；reason 区分错误子类。

    判定顺序：
    - MaxTurnsExceededError → reason="max_turns"（runner 现状在 _drive_turns 内抛，
      被外层 except AgentError 收口成 status="failed"，故 max_turns 真实输入是
      failed + MaxTurnsExceededError）。
    - ProviderError → 按 ``details["status_code"]`` 细分 llm.* reason。
    - ToolError → reason="tool.execution"。
    - None / 未知子类 → reason="internal.bug"。
    """
    error = result.error
    if error is None:
        return Disposition(action="deliver_up", reason="internal.bug")
    if isinstance(error, MaxTurnsExceededError):
        return Disposition(action="deliver_up", reason="max_turns")
    if isinstance(error, ProviderError):
        return Disposition(action="deliver_up", reason=_provider_error_reason(error))
    if isinstance(error, ToolError):
        return Disposition(action="deliver_up", reason="tool.execution")
    # 其余 AgentError 子类（ApprovalRejected / CapabilityDenied /
    # PermissionDenied / ConfigError 等）及未知类型 → internal.bug。
    return Disposition(action="deliver_up", reason="internal.bug")


def _provider_error_reason(error: ProviderError) -> Reason:
    """按 ProviderError.details 的 HTTP status_code 细分 llm.* reason。

    事实源：``docs/research/claude错误分类器.md`` —— provider 构造 ProviderError 时
    在 details 写 ``status_code``（anthropic_messages.py / openai_responses.py 现状）。
    无 status_code 时按 claude 默认归类 model_error/blocking_limit → llm.capability。
    """
    status_code = error.details.get("status_code")
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        # bool 是 int 子类，先排除；非 int status_code → 默认 capability。
        return "llm.capability"
    if status_code in (401, 403):
        return "llm.auth"
    if status_code == 429:
        return "llm.rate_limit"
    if status_code == 400:
        return "llm.protocol_400"
    # 5xx 及其余 → llm.capability（服务端过载 / model_error / blocking_limit）。
    return "llm.capability"


# ---------------------------------------------------------------------------
# build_mail 助手（被 task-4 agent_loop / task-5 AgentManager 调用）
# ---------------------------------------------------------------------------
#
# 单一入口（替代原 deliver_up / deliver_failure_up / deliver_partial_up /
# deliver_cancelled_up 4 个函数）：按 disposition.reason 决定 payload，
# 消费侧不再 isinstance 分发到不同助手。
#
# Mail 类型在 task-4（core.mail，模块 C）定义，本模块用 TYPE_CHECKING 前向引用：
# 运行时不 import task-4，避免 core → mailbox 反向依赖（import-linter Contract 1）。
# 返回类型用字符串注解 "Mail"，from __future__ import annotations 让其不在运行期解析。


def build_mail(
    disposition: Disposition,
    *,
    result: Result,
    sender: str,
    recipient_agent_id: str,
    task_id: str,
    epoch: int,
) -> Mail:
    """按 :class:`Disposition` 构造上投 Mail（kind 固定 ``child_result``）。

    payload 按 reason 决定：
    - ``completed`` → 取 ``result.final_message``（子走完全部 turn 的最终 assistant
      消息）；None 时退化空 assistant 消息。
    - ``max_turns`` / ``token_limit`` / ``cost_limit`` → 取 ``result.final_message``，
      metadata 附带 ``exhausted_reason`` 标记。
    - ``llm.*`` / ``tool.execution`` / ``internal.bug`` → assistant 消息承载 reason
      标签（``[child failed: {reason}]``）。
    - cancel 类 reason(``user_interrupt`` 等)→ assistant 消息承载 interrupted notice
      (``[child interrupted: {reason}]``);仅局部 cancel(action=deliver_up)会进本
      函数,树级 cancel(action=emit_only)消费侧不调本函数。
      文案用 interrupted(用户面语义),metadata 键 ``child_cancel_reason`` 保留 cancel
      词(数据契约字段名,与 ``Result.cancel_reason`` 同族,不破坏序列化)。

    Args:
        disposition: classify_result 产出（action 应为 deliver_up；emit_only 时调用
            方不该调本函数——树级取消不上投）。
        result: 子 run 的 Result。
        sender: 发送者 agent_id（子 agent）。
        recipient_agent_id: 接收者 agent_id（父 agent）。
        task_id: 关联 spawn 的 TaskRecord.task_id。
        epoch: 产生该 mail 的 run 起始 epoch（user_message 不过门卫）。
    """

    reason = disposition.reason
    payload = _with_child_run_metadata(_build_payload(reason, result), result)
    return _build_mail(
        kind="child_result",
        sender=sender,
        recipient_agent_id=recipient_agent_id,
        task_id=task_id,
        epoch=epoch,
        payload=payload,
    )


def _with_child_run_metadata(payload: Message, result: Result) -> Message:
    """补齐 child 运行摘要，输入为 payload/Result，输出为可供父级消费的 Message。"""
    additions: dict[str, object] = {}
    usage = result.metadata.get("usage")
    if isinstance(usage, dict):
        additions["usage"] = dict(usage)
    if result.turn_count >= 0:
        additions["turn_count"] = result.turn_count
    if not additions:
        return payload
    from core.message import Message

    return Message(
        role=payload.role,
        content=payload.content,
        tool_calls=payload.tool_calls,
        tool_call_id=payload.tool_call_id,
        name=payload.name,
        metadata={**payload.metadata, **additions},
    )


def _build_payload(reason: Reason, result: Result) -> Message:
    """按 reason 构造 Mail payload（Message），输入 reason + Result，输出 Message。

    成功类（completed）取 final_message；耗尽类附 exhausted_reason metadata；
    失败 / cancel 类合成 notice 消息。None final_message 一律退化空 assistant。
    """
    from core.message import Message

    if reason == "completed":
        payload = result.final_message
        if payload is None:
            return Message(role="assistant", content="")
        return payload

    if reason in ("max_turns", "token_limit", "cost_limit"):
        payload = result.final_message or Message(role="assistant", content="")
        # Message frozen，需带 exhausted_reason 时重建一条。
        if payload.metadata.get("exhausted_reason") is None:
            return Message(
                role=payload.role,
                content=payload.content,
                tool_calls=payload.tool_calls,
                tool_call_id=payload.tool_call_id,
                name=payload.name,
                metadata={**payload.metadata, "exhausted_reason": reason},
            )
        return payload

    if reason in ("user_interrupt", "parent_cascade", "hook_blocked", "watchdog"):
        # 局部 cancel 上投 interrupted notice(树级 cancel 不进本函数)。
        # 文案用 interrupted(用户面语义,与 CLI [interrupted] / WS RunInterruptedFrame
        # 对齐);metadata 键保留 cancel_reason(数据契约,test_outcome.py 断言)。
        return Message(
            role="assistant",
            content=f"[child interrupted: {reason}]",
            metadata={"child_cancel_reason": reason},
        )

    # llm.* / tool.execution / internal.bug → 失败通知。
    return Message(
        role="assistant",
        content=f"[child failed: {reason}]",
        metadata={"child_error_reason": reason},
    )


def _build_mail(
    *,
    kind: Literal["user_message", "child_result", "system_notice"],
    sender: str,
    recipient_agent_id: str,
    task_id: str,
    epoch: int,
    payload: Message,
) -> Mail:
    """构造 Mail（运行时从 core.mail 延迟 import）。

    task-4 的 core.mail.Mail 尚未实现，这里用延迟 import 避免在 task-2 阶段
    产生运行期反向依赖。task-4 合并 core.mail 后此函数自动可用；在此之前
    build_mail 调用会在运行期抛 ImportError（类型检查期签名已就绪）。
    """
    from core.mail import Mail

    return Mail(
        kind=kind,
        sender=sender,
        recipient_agent_id=recipient_agent_id,
        task_id=task_id,
        epoch=epoch,
        payload=payload,
    )


__all__ = [
    "Action",
    "Disposition",
    "Reason",
    "build_mail",
    "classify_result",
]
