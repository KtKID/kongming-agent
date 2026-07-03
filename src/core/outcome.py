"""Outcome 分类器（模块 D）。

把 runner 收口后的 :class:`Result`（status=completed/failed/cancelled + metadata）
翻译成封闭枚举 :class:`Outcome`，让 cancelled 从「异常控制流」剥成「数据值」。

设计要点（见 ``docs/spec/agent-tree-v0.1/04-data-and-state.md`` Outcome 定义 +
``docs/research/claude错误分类器.md`` query.ts reason 表）：

- Outcome 是独立 frozen dataclass 封闭枚举（不塞回 Result），4 个子类携带异构字段。
- :func:`classify` 是纯函数：吃 ``Result`` + ``current_tree_epoch``（仅辅助
  cancelled 的 tree_wide 推荐值），返回 ``Outcome``；无 IO、无副作用、永不抛异常
  （覆盖不到的组合兜底 :class:`Failed` (``internal.bug``)，保证 agent_loop match 不落空）。
- ``deliver_*`` 是上投 Mail 的构造助手，被 task-4 agent_loop 消费；本模块只 import
  core 自身类型，Mail 类型在 task-4 定义，用 ``TYPE_CHECKING`` 前向引用避免反向依赖。

边界内外分工：边界以内（provider/tool/runner）已用 try/finally 清理资源 + 翻译成
Result；本模块是 runner 之上的翻译层；边界以外只有 agent_loop 一个 match 消费点。
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
# 字面量枚举（事实源：docs/spec/agent-tree-v0.1/04-data-and-state.md）
# ---------------------------------------------------------------------------

# Exhausted.budget —— 哪种预算耗尽
Budget = Literal["max_turns", "token_limit", "cost_limit"]

# Cancelled.reason —— 取消触发来源
CancelReason = Literal["user_interrupt", "parent_cascade", "hook_blocked", "watchdog"]

# Failed.error_class —— 失败处置分类
ErrorClass = Literal[
    "llm.capability",
    "llm.protocol_400",
    "llm.auth",
    "llm.rate_limit",
    "tool.execution",
    "internal.bug",
]


# ---------------------------------------------------------------------------
# Outcome 封闭枚举（frozen dataclass）
# ---------------------------------------------------------------------------


class Outcome:
    """封闭枚举基类。

    子类（且仅这些子类）构成封闭集合，是 agent_loop match 的单一事实源：

    - :class:`Completed`：run 走完全部 turn，正常返回最终 assistant 消息。
    - :class:`Exhausted`：预算（max_turns / token / cost）耗尽，产出部分结果。
    - :class:`Cancelled`：runner 被外部 cancel（用户打断 / 父级连带 / hook 拦截 /
      看门狗），携带 ``tree_wide`` 标记指示是否树级取消。
    - :class:`Failed`：捕获到错误，按处置方式分类（llm.* / tool.execution /
      internal.bug）。

    基类本身不暴露构造入口（不写 ``@dataclass``），封闭性由「子类集合固定」保证。
    """


@dataclass(frozen=True)
class Completed(Outcome):
    """run 走完全部 turn，正常返回最终 assistant 消息。

    处置：``deliver_up``（子结果回灌父 mailbox）。
    """


@dataclass(frozen=True)
class Exhausted(Outcome):
    """预算耗尽，产出部分结果。

    Attributes:
        budget: 哪种预算耗尽：``max_turns`` / ``token_limit`` / ``cost_limit``。

    处置：``deliver_partial_up``。
    """

    budget: Budget


@dataclass(frozen=True)
class Cancelled(Outcome):
    """runner 被外部 cancel。

    Attributes:
        reason: 触发来源。``user_interrupt``（用户点 Stop）/ ``parent_cascade``
            （父被砍连带自己）/ ``hook_blocked``（lifecycle hook 拦截）/
            ``watchdog``（看门狗超时）。
        tree_wide: **classify 给的推荐值，最终判定权在 task-4 消费侧**。
            ``True`` = 父也被砍、不上投（emit_only）；``False`` = 局部取消、
            必须上投通知父（deliver_up）。推荐值按 reason 静态表（见
            :data:`_TREE_WIDE_RECOMMENDATION`），agent_loop 可按「是否树级
            cancel_subtree」覆盖。

    处置：``tree_wide=True`` → emit_only；``tree_wide=False`` → deliver_up。
    """

    reason: CancelReason
    tree_wide: bool


@dataclass(frozen=True)
class Failed(Outcome):
    """捕获到错误，按处置方式分类。

    Attributes:
        error_class: 失败处置分类（按「处置方式差异」合并，不按「错误从哪来」
            1:1 映射）：``llm.capability`` / ``llm.protocol_400`` / ``llm.auth`` /
            ``llm.rate_limit`` / ``tool.execution`` / ``internal.bug``。

    处置：``deliver_failure_up``。
    """

    error_class: ErrorClass


# ---------------------------------------------------------------------------
# classify 纯函数
# ---------------------------------------------------------------------------

# tree_wide 推荐值静态表（classify 产出，消费侧 task-4 可覆盖）。
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

# cancel_reason → Cancelled.reason 映射；未在表内的 reason 兜底 user_interrupt。
# runner 现状（runner.py:428）写 "user_interrupt"；task-3/4 引入 parent_cascade /
# hook_blocked / watchdog 时复用同一 metadata["cancel_reason"] 键。
_CANCEL_REASON_MAP: dict[str, CancelReason] = {
    "user_interrupt": "user_interrupt",
    "parent_cascade": "parent_cascade",
    "hook_blocked": "hook_blocked",
    "watchdog": "watchdog",
}


def classify(result: Result, current_tree_epoch: int) -> Outcome:
    """把 :class:`Result` 翻译成封闭枚举 :class:`Outcome`。

    纯函数：无 IO、无副作用、无对 mailbox/registry/ThreadCell 的依赖；
    永不抛异常，所有覆盖不到的组合兜底 :class:`Failed` (``internal.bug``)。

    判定优先级（README 技术设计段）：

    1. ``status == "completed"`` → 检查是否 max_turns 打满（用
       ``isinstance(result.error, MaxTurnsExceededError)`` 判定）：是 →
       :class:`Exhausted` (``max_turns``)；否 → :class:`Completed`。
    2. ``status == "cancelled"`` → 按 ``metadata["cancel_reason"]`` 映射
       :class:`Cancelled`.reason（未知 / 缺失兜底 ``user_interrupt``）+ tree_wide
       推荐值。
    3. ``status == "failed"`` → 按 ``type(result.error)`` 子类映射
       :class:`Failed`.error_class（ProviderError → 按 HTTP status_code 细分
       llm.*；ToolError → tool.execution；None / 未知 → internal.bug）。
    4. 兜底 :class:`Failed` (``internal.bug``)（未知 status 等，永不抛异常）。

    Args:
        result: runner 收口后的统一结果。
        current_tree_epoch: agent_loop 启动参数（世代计数器）。仅用于辅助 cancelled
            的 tree_wide 推荐值记录（当前实现推荐值是按 reason 的静态表，本参数不
            改变推荐值）；不读全局状态，避免内核反向依赖装配层。

    Returns:
        4 个 Outcome 子类之一（封闭集合）。
    """
    # 整个 classify 包一层兜底：即便出现意外异常也绝不向上抛，保证 agent_loop
    # match 不落空（agent_loop 不应因分类器自身 bug 而崩溃）。
    try:
        # 按 str 比较，避免 mypy 基于 ResultStatus Literal 收窄后把兜底分支判为
        # unreachable —— 运行期可能通过 cast/反射写入非标准 status，兜底必须可达。
        status: str = result.status
    except Exception:
        return Failed("internal.bug")

    if status == "completed":
        return _classify_completed(result)
    if status == "cancelled":
        return _classify_cancelled(result, current_tree_epoch)
    if status == "failed":
        return _classify_failed(result)
    # 理论不会出现（ResultStatus 只有 3 种），兜底 internal.bug。
    return Failed("internal.bug")


def _classify_completed(result: Result) -> Outcome:
    """status=completed：MaxTurnsExceededError → Exhausted，否则 Completed。

    注：runner 现状中 max_turns 打满走的是 ``status="failed"`` 且
    ``error=MaxTurnsExceededError``（runner.py:624）。这里同时兼容 completed +
    error=MaxTurnsExceededError 的写法（按 README reason 映射表「status=completed
    且 turn_count 打满 max_turns，或 status=failed 且 error 是
    MaxTurnsExceededError」均归 Exhausted）。无 error 时默认 Completed
    （无法判定打满）。
    """
    if isinstance(result.error, MaxTurnsExceededError):
        return Exhausted("max_turns")
    return Completed()


def _classify_cancelled(result: Result, current_tree_epoch: int) -> Cancelled:
    """status=cancelled：按 metadata["cancel_reason"] 映射 reason + tree_wide 推荐值。

    - 未知 / 缺失 / 空串 reason → 兜底 ``user_interrupt``（runner 现状默认值）。
    - tree_wide 取 ``_TREE_WIDE_RECOMMENDATION`` 静态表；mapped reason 不在表
      时兜底 True（user_interrupt 的推荐值）。
    - ``current_tree_epoch`` 仅记录用，当前不改变推荐值（推荐值是按 reason 的
      静态表）；保留参数以对齐 agent_loop 调用签名 + 为 task-4 覆盖逻辑留钩子。
    """
    metadata = result.metadata or {}
    raw_reason = metadata.get("cancel_reason", "")
    # metadata 是 dict[str, object]，取出的值是 object，需转 str 兜底。
    raw_reason_str = raw_reason if isinstance(raw_reason, str) else ""
    reason = _CANCEL_REASON_MAP.get(raw_reason_str, "user_interrupt")
    tree_wide = _TREE_WIDE_RECOMMENDATION.get(reason, True)
    return Cancelled(reason=reason, tree_wide=tree_wide)


def _classify_failed(result: Result) -> Outcome:
    """status=failed：按 type(result.error) 子类映射 Outcome。

    判定顺序：
    - MaxTurnsExceededError → Exhausted(max_turns)。**runner 现状（runner.py:653）
      在 _drive_turns 内抛 MaxTurnsExceededError，被外层 except AgentError 收口成
      status="failed"**（runner.py:447）。故 max_turns 打满的真实输入是 failed +
      MaxTurnsExceededError，必须在此优先判出 Exhausted（对齐 README reason 映射表）。
    - ProviderError → 按 ``details["status_code"]`` 细分 Failed(llm.*)：
        - 401 / 403 → llm.auth（鉴权失败）
        - 429 → llm.rate_limit（限流）
        - 400 → llm.protocol_400（prompt_too_long 等协议错误）
        - 5xx / 无 status_code / 其他 → llm.capability（model_error /
          blocking_limit / 服务端过载等默认归类）
    - ToolError → Failed(tool.execution)
    - None / 未知子类 → Failed(internal.bug)
    """
    error = result.error
    if error is None:
        return Failed("internal.bug")
    if isinstance(error, MaxTurnsExceededError):
        return Exhausted("max_turns")
    if isinstance(error, ProviderError):
        return Failed(_provider_error_class(error))
    if isinstance(error, ToolError):
        return Failed("tool.execution")
    # 其余 AgentError 子类（ApprovalRejected / CapabilityDenied /
    # PermissionDenied / ConfigError 等）及未知类型 → internal.bug。
    return Failed("internal.bug")


def _provider_error_class(error: ProviderError) -> ErrorClass:
    """按 ProviderError.details 的 HTTP status_code 细分 llm.* error_class。

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
# deliver 助手（被 task-4 agent_loop 调用）
# ---------------------------------------------------------------------------
#
# 本 task 建签名 + Completed/Failed/Exhausted 的 Mail 构造；Cancelled 的 deliver
# 留 task-4 agent_loop（Cancelled 的 tree_wide 最终判定在消费侧）。
#
# Mail 类型在 task-4（core.mail，模块 C）定义，本模块用 TYPE_CHECKING 前向引用：
# 运行时不 import task-4，避免 core → mailbox 反向依赖（import-linter Contract 1）。
# 返回类型用字符串注解 "Mail"，from __future__ import annotations 让其不在运行期解析。


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
    deliver_* 调用会在运行期抛 ImportError（类型检查期签名已就绪）。
    """
    # 延迟 import：Mail 在 task-4 的 core.mail 定义。放函数内而非模块顶，
    # 保证 import core.outcome 本身不触发对 task-4 的依赖。task-4 合并后此 import
    # 自动可用；在此之前调用会运行期 ModuleNotFoundError（类型检查期签名已就绪）。
    from core.mail import Mail

    return Mail(
        kind=kind,
        sender=sender,
        recipient_agent_id=recipient_agent_id,
        task_id=task_id,
        epoch=epoch,
        payload=payload,
    )


def deliver_up(
    outcome: Completed,
    *,
    result: Result,
    sender: str,
    recipient_agent_id: str,
    task_id: str,
    epoch: int,
) -> Mail:
    """Completed 上投：子结果回灌父 mailbox（kind=child_result）。

    payload 取 ``result.final_message``（子 agent 走完全部 turn 的最终 assistant
    消息）；若 final_message 为 None（理论不会，Completed 必有产出），退化为空
    assistant 消息，保证 Mail.payload 非 None。

    Args:
        outcome: Completed（仅类型占位，Completed 无字段，目前不消费）。
        result: 子 run 的 Result。
        sender: 发送者 agent_id（子 agent）。
        recipient_agent_id: 接收者 agent_id（父 agent）。
        task_id: 关联 spawn 的 TaskRecord.task_id。
        epoch: 产生该 mail 的 run 起始 epoch（user_message 不过门卫）。
    """
    payload = result.final_message
    if payload is None:
        # Completed 必有 final_message；防御性兜底，避免 Mail.payload=None。
        from core.message import Message

        payload = Message(role="assistant", content="")
    return _build_mail(
        kind="child_result",
        sender=sender,
        recipient_agent_id=recipient_agent_id,
        task_id=task_id,
        epoch=epoch,
        payload=payload,
    )


def deliver_failure_up(
    outcome: Failed,
    *,
    sender: str,
    recipient_agent_id: str,
    task_id: str,
    epoch: int,
) -> Mail:
    """Failed 上投：失败通知父（kind=child_result，payload 是失败 system notice）。

    payload 用一条 assistant 消息承载 ``error_class`` + 原始 error message，
    便于父 agent 在对话历史中看到子失败原因。原始 AgentError 文本通过
    ``outcome.error_class`` 之外的 message 字段附带（Failed 不携带原始 error 引用，
    避免长生命周期持有异常对象；父侧按 error_class 决定处置即可）。
    """
    from core.message import Message

    payload = Message(
        role="assistant",
        content=f"[child failed: {outcome.error_class}]",
        metadata={"child_error_class": outcome.error_class},
    )
    return _build_mail(
        kind="child_result",
        sender=sender,
        recipient_agent_id=recipient_agent_id,
        task_id=task_id,
        epoch=epoch,
        payload=payload,
    )


def deliver_partial_up(
    outcome: Exhausted,
    *,
    result: Result,
    sender: str,
    recipient_agent_id: str,
    task_id: str,
    epoch: int,
) -> Mail:
    """Exhausted 上投：预算耗尽的部分结果（kind=child_result）。

    payload 取 ``result.final_message``（max_turns 打满时 runner 保留的已有消息）；
    为 None 时退化空 assistant 消息。metadata 附带 ``budget`` 标记，便于父侧
    区分「子完成」与「子预算耗尽」。
    """
    from core.message import Message

    payload = result.final_message or Message(role="assistant", content="")
    # 不直接改 payload（Message frozen），需要带 budget 时重建一条。
    if payload.metadata.get("exhausted_budget") is None:
        payload = Message(
            role=payload.role,
            content=payload.content,
            tool_calls=payload.tool_calls,
            tool_call_id=payload.tool_call_id,
            name=payload.name,
            metadata={**payload.metadata, "exhausted_budget": outcome.budget},
        )
    return _build_mail(
        kind="child_result",
        sender=sender,
        recipient_agent_id=recipient_agent_id,
        task_id=task_id,
        epoch=epoch,
        payload=payload,
    )


def deliver_cancelled_up(
    outcome: Cancelled,
    *,
    sender: str,
    recipient_agent_id: str,
    task_id: str,
    epoch: int,
) -> Mail:
    """Cancelled 上投：tree_wide=False（局部取消）时的失败通知父。

    .. note::

        **本函数留待 task-4 agent_loop 消费侧实现**。Cancelled 的 ``tree_wide``
        最终判定权在消费侧（task-4）：classify 只按 reason 给推荐值（见
        :data:`_TREE_WIDE_RECOMMENDATION`），agent_loop 需按「是否树级
        cancel_subtree」决定覆盖为 False 还是保持 True（True 时走 emit_only，
        不上投，本函数不调用）。

        本 task 只产出签名 + 文档约定，构造逻辑在 task-4 agent_loop 中补齐
        （Mail 构造 + Cancelled.reason / tree_wide 的最终值）。
    """
    raise NotImplementedError(
        "Cancelled 的 deliver 留 task-4 agent_loop：tree_wide 最终判定在消费侧，"
        "classify 只给推荐值。"
    )


__all__ = [
    "Budget",
    "CancelReason",
    "Cancelled",
    "Completed",
    "ErrorClass",
    "Exhausted",
    "Failed",
    "Outcome",
    "classify",
    "deliver_cancelled_up",
    "deliver_failure_up",
    "deliver_partial_up",
    "deliver_up",
]
