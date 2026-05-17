"""scheduler v0.1 — 领域层（数据真源，零运行时判定）。

本文件严格遵守 ``docs/agent-cron-module-v0.1/04-data-and-state.md``：

- 所有 dataclass ``frozen=True``
- 所有枚举继承 :class:`enum.StrEnum`
- 仅承载数据形态，不放任何持久化 / trigger 计算 / approval 决策逻辑
- ``__post_init__`` 只做关键不变量校验，不触发 IO / logging
- v0.1 **显式不引入** ``waiting_approval`` 状态：cron run 命中未授权动作直接
  ``RunStatus.FAILED`` + ``RunFailureReason.NEEDS_APPROVAL``，下次 trigger 重跑

模块边界（参见文档 §7.1 Domain Model 边界）：
- 允许定义字段、枚举和不变量
- 不允许直接访问数据库
- 不允许引用 APScheduler 类型
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# 常量（参见文档 §9）
# ---------------------------------------------------------------------------


ONESHOT_GRACE_SECONDS = 120
"""一次性任务边界宽限秒数（参考 Hermes）。"""

DEFAULT_INACTIVITY_TIMEOUT = 600
"""默认 inactivity 超时秒数：连续 10 min 无活动则强制取消。"""

GRACE_MIN_SECONDS = 120
"""missed-run 快进 grace 下限。"""

GRACE_MAX_SECONDS = 7200
"""missed-run 快进 grace 上限（2 小时）。"""

SILENT_MARKER = "[SILENT]"
"""final message 前缀触发投递抑制的标记。"""

SCHEMA_VERSION = 5
"""``scheduled_tasks.json`` / ``runs/*.jsonl`` 的持久化 schema 版本。

v0.2 重构：引入 :attr:`TriggerType.SECONDS` 等新枚举值，**不兼容 v0.1 数据**。

v0.3 新增（cron-delivery-v0.3）：``ScheduleDelivery`` 配置 +
:class:`ScheduledRun` 的 ``delivered_at`` / ``delivery_status`` / ``seen_at``
字段 + :class:`TaskState.DELETED` 软删除态。**不兼容 v0.2 数据**。

v0.4 新增（cron-thread-preset-v0.4）：``ScheduleDelivery.target`` 投递目标 +
``ScheduledTask.preset_id`` LLM 选择。**不兼容 v0.3 数据**；
旧版由 Store 启动时重置式迁移（备份 + 写空 v4）。

v0.5 新增（scheduler-approval-task-level-v0.5）：``TaskExecutionPolicy``
新增 ``approval_mode`` 字段（默认 None）。**不兼容 v0.4 数据**；
旧版由 Store 启动时重置式迁移（备份 + 写空 v5）。
"""


# ---------------------------------------------------------------------------
# 核心枚举（参见文档 §2）
# ---------------------------------------------------------------------------


class TriggerType(StrEnum):
    """触发器类型。

    - ``ONCE``: ``expr`` 是 ISO timestamp
    - ``INTERVAL``: ``expr`` 是规范化间隔表达，如 ``PT30M``（v0.1 简化口径下也允许
      纯分钟整数文本）
    - ``CRON``: ``expr`` 是 5/6-field cron expression
    - ``SECONDS``: v0.2 新增；``expr`` 是正整数秒数文本，如 ``"10"``
    """

    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"
    SECONDS = "seconds"


class TaskState(StrEnum):
    """任务级状态（用于展示与恢复）。

    状态约束：``enabled=False`` 时只能是 ``PAUSED`` / ``DISABLED`` /
    ``COMPLETED`` / ``DELETED``。

    v0.3 新增 ``DELETED``：软删除态。Web ``/cron`` 列表 API 默认过滤；
    runs 历史保留以便审计。
    """

    SCHEDULED = "scheduled"
    PAUSED = "paused"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DISABLED = "disabled"
    DELETED = "deleted"


class RunStatus(StrEnum):
    """单次执行状态。

    - ``RUNNING``: 在跑
    - ``COMPLETED``: 正常结束
    - ``SILENT``: final message 命中 ``[SILENT]``，仅落审计、不投递
    - ``FAILED``: ``failure_reason`` 必填
    - ``INACTIVITY_TIMEOUT``: 卡死兜底（与 ``FAILED`` 分开统计）
    - ``ABANDONED``: 进程崩溃 / 重启后旧 run 收尾
    - ``CANCELLED``: replace 策略下被新 run 取代
    """

    RUNNING = "running"
    COMPLETED = "completed"
    SILENT = "silent"
    FAILED = "failed"
    INACTIVITY_TIMEOUT = "inactivity_timeout"
    ABANDONED = "abandoned"
    CANCELLED = "cancelled"


class RunFailureReason(StrEnum):
    """``RunStatus.FAILED`` 等失败状态的归因。"""

    NEEDS_APPROVAL = "needs_approval"
    INACTIVITY_TIMEOUT = "inactivity_timeout"
    TOOL_ERROR = "tool_error"
    RUNNER_EXCEPTION = "runner_exception"
    ABANDONED_ON_RESTART = "abandoned_on_restart"
    DELIVERY_ERROR = "delivery_error"


class DeliveryChannel(StrEnum):
    """v0.3 新增：cron 投递渠道。

    - ``WEB``: 投递到 web 系统消息池（``/cron`` 触发记录 tab + WS 广播）
    - ``CLI``: 投递到 CLI（下次提示符前 flush）

    扩展点：远端 channel（feishu / email / Slack 等）暂不实现，
    保留枚举位以便后续扩展时不破坏 schema。
    """

    WEB = "web"
    CLI = "cli"


class DeliveryStatus(StrEnum):
    """v0.3 新增：单次 :class:`ScheduledRun` 的 delivery 阶段状态。

    - ``PENDING``: 创建时默认值；run 还未跑完
    - ``DELIVERED``: dispatcher 投递成功
    - ``FAILED``: 投递失败（``ScheduledRun.delivery_error`` 含错误信息）
    - ``SKIPPED``: 命中 ``[SILENT]`` 静默标记 / 没有对应 sink / 任务已 deleted

    与 :class:`RunStatus`（agent run 自身状态）正交：``status=COMPLETED``
    但 ``delivery_status=FAILED`` 是合法状态（agent 跑完了但投递阶段挂了）。
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    SKIPPED = "skipped"


class SessionMode(StrEnum):
    """session 复用模式。v0.1 第一版固定 ``FRESH_SESSION``。"""

    FRESH_SESSION = "fresh_session"


class ConcurrencyPolicy(StrEnum):
    """并发策略。

    - ``FORBID``: 已有运行中任务时直接跳过
    - ``ALLOW``: 允许并发运行
    - ``REPLACE``: 取消旧 run，启动新 run
    """

    FORBID = "forbid"
    ALLOW = "allow"
    REPLACE = "replace"


class MisfirePolicy(StrEnum):
    """错过触发窗口后的处理策略。

    - ``SKIP``: 快进到未来，不补跑
    - ``CATCH_UP_ONCE``: 允许补 1 次
    - ``FIRE_NOW``: 立即执行 1 次
    """

    SKIP = "skip"
    CATCH_UP_ONCE = "catch_up_once"
    FIRE_NOW = "fire_now"


class ApprovalMode(StrEnum):
    """cron 任务的审批模式（v0.5 新增）。

    - FAIL_CLOSED: 当前行为；命中 explicit_consent → rejected
      （除 SchedulerApprovalConfig.allow_write_file_create_in_cwd 白名单）
    - TRUST: cron 信任模式；hard_block 仍 rejected，
      explicit_consent（standard / elevated）自动 approved，
      silent_allow / standard_allow 透传
    """

    FAIL_CLOSED = "fail_closed"
    TRUST = "trust"


class TaskOrigin(StrEnum):
    """任务来源入口。"""

    CLI = "cli"
    WEB = "web"
    TOOL = "tool"
    SYSTEM = "system"


# ---------------------------------------------------------------------------
# 内部工具：不变量校验（不依赖外部库）
# ---------------------------------------------------------------------------


_DISABLED_TASK_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.PAUSED,
        TaskState.DISABLED,
        TaskState.COMPLETED,
        TaskState.DELETED,  # v0.3 软删除
    }
)
"""``enabled=False`` 时允许的 :class:`TaskState` 集合。"""


def _ensure_iso_or_none(field_name: str, value: object) -> None:
    """字段必须是 ``str`` 或 ``None``。文档 §3 约定时间字段用 ISO8601 文本。

    本函数仅校验类型，不验证 ISO8601 内容（解析职责留给 store / trigger engine）。
    """
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{field_name} must be ISO8601 str or None, got {type(value).__name__}")


def _ensure_non_negative_or_none(field_name: str, value: object) -> None:
    """``int >= 0`` 或 ``None``。``None`` 表示禁用该维度。"""
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be int or None, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field_name} must be >= 0, got {value}")


# ---------------------------------------------------------------------------
# 领域结构（参见文档 §3）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleTrigger:
    """触发器定义（文档 §3.1）。

    - ``trigger_type=ONCE``: ``expr`` 是 ISO timestamp
    - ``trigger_type=INTERVAL``: ``expr`` 是规范化间隔表达，如 ``PT30M``
    - ``trigger_type=CRON``: ``expr`` 是 5-field cron expression
    - ``timezone``: IANA timezone string；所有 ``next_run_at`` 计算基于此时区

    边界：Trigger Engine 负责把它映射为 APScheduler trigger；Store 只持久化，
    不解释语义。
    """

    trigger_type: TriggerType
    expr: str
    timezone: str

    def __post_init__(self) -> None:
        if not isinstance(self.trigger_type, TriggerType):
            raise TypeError(
                f"trigger_type must be TriggerType, got {type(self.trigger_type).__name__}"
            )
        if not isinstance(self.expr, str) or not self.expr:
            raise ValueError("expr must be non-empty str")
        if not isinstance(self.timezone, str) or not self.timezone:
            raise ValueError("timezone must be non-empty IANA timezone str")
        if self.trigger_type is TriggerType.SECONDS:
            try:
                seconds = int(self.expr)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"SECONDS expr must be positive integer text, got {self.expr!r}"
                ) from exc
            if seconds <= 0:
                raise ValueError(f"SECONDS expr must be positive integer text, got {self.expr!r}")


@dataclass(frozen=True)
class TaskExecutionPolicy:
    """单任务执行策略（文档 §3.2）。

    - ``session_mode``: 第一版固定 :attr:`SessionMode.FRESH_SESSION`
    - ``concurrency_policy``: ``FORBID`` / ``ALLOW`` / ``REPLACE``
    - ``misfire_policy``: ``SKIP`` / ``CATCH_UP_ONCE`` / ``FIRE_NOW``
    - ``max_turns``: 覆盖 ``AgentSpec.max_turns``；``None`` 走 agent 默认
    - ``inactivity_timeout_seconds``: v0.1 主用超时维度，默认 600s；
      ``0`` / ``None`` 表示不启用
    - ``wall_timeout_seconds``: v0.1 字段保留但运行时不强制
    - ``retry_limit``: 调度级失败的最多重试次数（不重试 ``needs_approval``）
    - ``silent_marker_enabled``: 是否启用 ``[SILENT]`` 静默投递抑制；默认开启
    - ``approval_mode``: v0.5 新增；None 表示沿用全局
      ``SchedulerApprovalConfig.mode``，否则覆盖全局默认

    边界：Policy 由 Safety and Policy Integration 解释；Execution Bridge
    负责消费执行相关字段。
    """

    session_mode: SessionMode = SessionMode.FRESH_SESSION
    concurrency_policy: ConcurrencyPolicy = ConcurrencyPolicy.FORBID
    misfire_policy: MisfirePolicy = MisfirePolicy.SKIP
    max_turns: int | None = None
    inactivity_timeout_seconds: int | None = DEFAULT_INACTIVITY_TIMEOUT
    wall_timeout_seconds: int | None = None
    retry_limit: int = 0
    silent_marker_enabled: bool = True
    approval_mode: ApprovalMode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_mode, SessionMode):
            raise TypeError(
                f"session_mode must be SessionMode, got {type(self.session_mode).__name__}"
            )
        if not isinstance(self.concurrency_policy, ConcurrencyPolicy):
            raise TypeError(
                "concurrency_policy must be ConcurrencyPolicy, "
                f"got {type(self.concurrency_policy).__name__}"
            )
        if not isinstance(self.misfire_policy, MisfirePolicy):
            raise TypeError(
                f"misfire_policy must be MisfirePolicy, got {type(self.misfire_policy).__name__}"
            )
        _ensure_non_negative_or_none("max_turns", self.max_turns)
        _ensure_non_negative_or_none("inactivity_timeout_seconds", self.inactivity_timeout_seconds)
        _ensure_non_negative_or_none("wall_timeout_seconds", self.wall_timeout_seconds)
        if not isinstance(self.retry_limit, int) or isinstance(self.retry_limit, bool):
            raise TypeError(f"retry_limit must be int, got {type(self.retry_limit).__name__}")
        if self.retry_limit < 0:
            raise ValueError(f"retry_limit must be >= 0, got {self.retry_limit}")
        if not isinstance(self.silent_marker_enabled, bool):
            raise TypeError(
                "silent_marker_enabled must be bool, "
                f"got {type(self.silent_marker_enabled).__name__}"
            )
        if self.approval_mode is not None and not isinstance(self.approval_mode, ApprovalMode):
            raise TypeError(
                f"approval_mode must be ApprovalMode or None, "
                f"got {type(self.approval_mode).__name__}"
            )


@dataclass(frozen=True)
class TaskTarget:
    """任务投递目标（文档 §3.3）。

    - ``agent_name``: 指向目标 agent spec
    - ``input_text``: 实际传给 ``Runner.run(user_input=...)`` 的输入；
      必须是自包含任务描述
    - ``metadata``: 来源、展示名、标签、保守扩展字段

    边界：第一版不把 Python callable / shell snippet / 外部投递目标
    直接塞进本结构。
    """

    agent_name: str
    input_text: str
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.agent_name, str) or not self.agent_name:
            raise ValueError("agent_name must be non-empty str")
        if not isinstance(self.input_text, str):
            raise TypeError(f"input_text must be str, got {type(self.input_text).__name__}")
        if not isinstance(self.metadata, dict):
            raise TypeError(f"metadata must be dict[str, str], got {type(self.metadata).__name__}")
        for key, value in self.metadata.items():
            if not isinstance(key, str):
                raise TypeError(f"metadata key must be str, got {type(key).__name__}")
            if not isinstance(value, str):
                raise TypeError(f"metadata[{key!r}] must be str, got {type(value).__name__}")


@dataclass(frozen=True)
class ScheduleDelivery:
    """cron 任务触发后 ``final_message`` 的投递配置。

    - ``channel``: 广播渠道（web / cli）——始终走，决定 cron 区域的呈现方式
    - ``target``: 投递目标（v0.4 新增）——非空时额外投递到具体目标

    target 格式约定（带前缀字符串）：
    - ``None``: 只广播到 cron 区域，不投递到具体位置
    - ``"thread:<thread_id>"``: 投递到该 thread（追加消息 + WS 通知）
    - 未来扩展：``"feishu:<user_id>"`` 等（当前不实现）

    边界：仅承载配置数据；具体投递逻辑由 ``DeliveryDispatcher`` /
    ``WebDeliverySink`` / ``TargetDeliverySink`` 等运行时组件负责。
    """

    channel: DeliveryChannel
    target: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.channel, DeliveryChannel):
            raise TypeError(f"channel must be DeliveryChannel, got {type(self.channel).__name__}")
        if self.target is not None and not isinstance(self.target, str):
            raise TypeError(f"target must be str or None, got {type(self.target).__name__}")


@dataclass(frozen=True)
class ScheduledTask:
    """定时任务定义（文档 §3.4）。

    状态约束：
    - ``enabled=False`` 时 ``state`` 只能是 ``PAUSED`` / ``DISABLED`` / ``COMPLETED``
    - one-shot 完成后可转为 ``COMPLETED``
    - recurring task 正常执行后回到 ``SCHEDULED``

    时间字段（``next_run_at`` / ``last_run_at`` / ``created_at`` / ``updated_at``）
    用 ISO8601 文本表示；本类只校验类型，内容由 store / trigger engine 解析。
    """

    task_id: str
    name: str
    enabled: bool
    state: TaskState
    origin: TaskOrigin
    trigger: ScheduleTrigger
    policy: TaskExecutionPolicy
    target: TaskTarget
    next_run_at: str | None
    last_run_at: str | None
    created_by: str
    created_at: str
    updated_at: str
    delivery: ScheduleDelivery | None = None
    """cron 触发后投递配置；``None`` 表示沿用默认（web channel，无 target）。"""

    preset_id: str = ""
    """v0.4 新增：用哪个 LLM preset 执行。匹配 ``LLMPresetConfig.id``。
    空串表示用全局默认（base_config.model）。创建时从来源 thread 继承。"""

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("task_id must be non-empty str")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("name must be non-empty str")
        if not isinstance(self.enabled, bool):
            raise TypeError(f"enabled must be bool, got {type(self.enabled).__name__}")
        if not isinstance(self.state, TaskState):
            raise TypeError(f"state must be TaskState, got {type(self.state).__name__}")
        if not isinstance(self.origin, TaskOrigin):
            raise TypeError(f"origin must be TaskOrigin, got {type(self.origin).__name__}")
        if not isinstance(self.trigger, ScheduleTrigger):
            raise TypeError(f"trigger must be ScheduleTrigger, got {type(self.trigger).__name__}")
        if not isinstance(self.policy, TaskExecutionPolicy):
            raise TypeError(f"policy must be TaskExecutionPolicy, got {type(self.policy).__name__}")
        if not isinstance(self.target, TaskTarget):
            raise TypeError(f"target must be TaskTarget, got {type(self.target).__name__}")
        _ensure_iso_or_none("next_run_at", self.next_run_at)
        _ensure_iso_or_none("last_run_at", self.last_run_at)
        if not isinstance(self.created_by, str) or not self.created_by:
            raise ValueError("created_by must be non-empty str")
        _ensure_iso_or_none("created_at", self.created_at)
        if self.created_at is None:
            raise ValueError("created_at is required (ISO8601 str)")
        _ensure_iso_or_none("updated_at", self.updated_at)
        if self.updated_at is None:
            raise ValueError("updated_at is required (ISO8601 str)")
        if self.delivery is not None and not isinstance(self.delivery, ScheduleDelivery):
            raise TypeError(
                f"delivery must be ScheduleDelivery or None, got {type(self.delivery).__name__}"
            )
        if not isinstance(self.preset_id, str):
            raise TypeError(f"preset_id must be str, got {type(self.preset_id).__name__}")
        if not self.enabled and self.state not in _DISABLED_TASK_STATES:
            raise ValueError(
                "enabled=False requires state in "
                "{paused, disabled, completed, deleted}, "
                f"got {self.state.value!r}"
            )


@dataclass(frozen=True)
class ScheduledRun:
    """单次任务执行的运行记录（文档 §3.5）。

    - ``scheduled_for``: 这次执行原本对应的逻辑触发时间
    - ``session_id``: fresh session 标识
    - ``result_status``: 映射 ``Result.status``
    - ``final_message_excerpt``: 用于列表展示与审计；命中 ``[SILENT]`` 时
      仍写入（含 marker 本身）
    - ``failure_reason``: ``status`` 为 ``FAILED`` / ``INACTIVITY_TIMEOUT``
      / ``ABANDONED`` 时必填
    - ``delivery_error``: 仅记录"投递阶段"失败，不影响 agent 是否 success
    - ``silent_suppressed``: ``status=SILENT`` 或开启 ``silent_marker_enabled``
      且命中标记时为 True

    边界：本结构只保留摘要，不复制整段 session history。
    """

    run_id: str
    task_id: str
    status: RunStatus
    scheduled_for: str
    started_at: str | None
    finished_at: str | None
    session_id: str | None
    result_status: str | None
    final_message_excerpt: str | None
    error_message: str | None
    failure_reason: RunFailureReason | None
    delivery_error: str | None
    silent_suppressed: bool
    delivered_at: str | None = None
    """v0.3: 投递成功 ISO8601 时间戳；未投递 / 失败时为 None。"""
    delivery_status: DeliveryStatus = DeliveryStatus.PENDING
    """v0.3: 单次 run 的投递阶段状态；与 ``status`` 正交。"""
    seen_at: str | None = None
    """v0.3: 用户在 web ``/cron`` 看过该 run 的 ISO8601 时间；未看为 None。
    红点未读计数仅看 ``delivery_status==DELIVERED and seen_at is None`` 的 run。"""

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id must be non-empty str")
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("task_id must be non-empty str")
        if not isinstance(self.status, RunStatus):
            raise TypeError(f"status must be RunStatus, got {type(self.status).__name__}")
        _ensure_iso_or_none("scheduled_for", self.scheduled_for)
        if self.scheduled_for is None:
            raise ValueError("scheduled_for is required (ISO8601 str)")
        _ensure_iso_or_none("started_at", self.started_at)
        _ensure_iso_or_none("finished_at", self.finished_at)
        if self.session_id is not None and not isinstance(self.session_id, str):
            raise TypeError(f"session_id must be str or None, got {type(self.session_id).__name__}")
        if self.result_status is not None and not isinstance(self.result_status, str):
            raise TypeError(
                f"result_status must be str or None, got {type(self.result_status).__name__}"
            )
        if self.final_message_excerpt is not None and not isinstance(
            self.final_message_excerpt, str
        ):
            raise TypeError(
                "final_message_excerpt must be str or None, "
                f"got {type(self.final_message_excerpt).__name__}"
            )
        if self.error_message is not None and not isinstance(self.error_message, str):
            raise TypeError(
                f"error_message must be str or None, got {type(self.error_message).__name__}"
            )
        if self.failure_reason is not None and not isinstance(
            self.failure_reason, RunFailureReason
        ):
            raise TypeError(
                "failure_reason must be RunFailureReason or None, "
                f"got {type(self.failure_reason).__name__}"
            )
        if self.delivery_error is not None and not isinstance(self.delivery_error, str):
            raise TypeError(
                f"delivery_error must be str or None, got {type(self.delivery_error).__name__}"
            )
        if not isinstance(self.silent_suppressed, bool):
            raise TypeError(
                f"silent_suppressed must be bool, got {type(self.silent_suppressed).__name__}"
            )
        # v0.3 新增字段校验
        _ensure_iso_or_none("delivered_at", self.delivered_at)
        if not isinstance(self.delivery_status, DeliveryStatus):
            raise TypeError(
                f"delivery_status must be DeliveryStatus, got {type(self.delivery_status).__name__}"
            )
        _ensure_iso_or_none("seen_at", self.seen_at)


# ---------------------------------------------------------------------------
# 运行时结构（参见文档 §5）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DueTaskReservation:
    """due-task 预占结构（文档 §5.1）。

    用途：Trigger Engine 从 Store 拿到 due task 后，交给 Execution Bridge；
    表示这次执行窗口已经被当前进程占用。
    """

    task: ScheduledTask
    scheduled_for: str
    reserved_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.task, ScheduledTask):
            raise TypeError(f"task must be ScheduledTask, got {type(self.task).__name__}")
        _ensure_iso_or_none("scheduled_for", self.scheduled_for)
        if self.scheduled_for is None:
            raise ValueError("scheduled_for is required (ISO8601 str)")
        _ensure_iso_or_none("reserved_at", self.reserved_at)
        if self.reserved_at is None:
            raise ValueError("reserved_at is required (ISO8601 str)")


@dataclass(frozen=True)
class TaskExecutionContext:
    """任务执行上下文（文档 §5.2）。

    用途：显式传入 Execution Bridge 和 runtime；替代 Hermes 的环境变量注入。
    边界：只传确定性上下文，不携带跨模块可变全局状态。
    """

    task_id: str
    scheduled_for: str
    trigger_type: TriggerType
    origin: TaskOrigin
    is_scheduled_run: bool

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("task_id must be non-empty str")
        _ensure_iso_or_none("scheduled_for", self.scheduled_for)
        if self.scheduled_for is None:
            raise ValueError("scheduled_for is required (ISO8601 str)")
        if not isinstance(self.trigger_type, TriggerType):
            raise TypeError(
                f"trigger_type must be TriggerType, got {type(self.trigger_type).__name__}"
            )
        if not isinstance(self.origin, TaskOrigin):
            raise TypeError(f"origin must be TaskOrigin, got {type(self.origin).__name__}")
        if not isinstance(self.is_scheduled_run, bool):
            raise TypeError(
                f"is_scheduled_run must be bool, got {type(self.is_scheduled_run).__name__}"
            )


@dataclass(frozen=True)
class ScheduledRunRequest:
    """Execution Bridge 装配 ``Runner.run()`` 一次调用的内部入参（文档 §5.3）。

    边界：``Runner`` 继续消费现有参数，不要求 core 立即引入新公共协议；
    本结构仅在 scheduler 模块内部传递。
    """

    user_input: str
    agent_name: str
    session_id: str
    run_id: str
    max_turns_override: int | None
    execution_context: TaskExecutionContext

    def __post_init__(self) -> None:
        if not isinstance(self.user_input, str):
            raise TypeError(f"user_input must be str, got {type(self.user_input).__name__}")
        if not isinstance(self.agent_name, str) or not self.agent_name:
            raise ValueError("agent_name must be non-empty str")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id must be non-empty str")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id must be non-empty str")
        _ensure_non_negative_or_none("max_turns_override", self.max_turns_override)
        if not isinstance(self.execution_context, TaskExecutionContext):
            raise TypeError(
                "execution_context must be TaskExecutionContext, "
                f"got {type(self.execution_context).__name__}"
            )


# ---------------------------------------------------------------------------
# 辅助函数（v0.5）
# ---------------------------------------------------------------------------


def resolve_effective_mode(
    task_mode: ApprovalMode | None,
    global_mode: ApprovalMode,
) -> ApprovalMode:
    """优先级解析：task 显式声明优先，否则全局兜底（v0.5 新增）。"""
    return task_mode if task_mode is not None else global_mode


__all__ = [
    "DEFAULT_INACTIVITY_TIMEOUT",
    "GRACE_MAX_SECONDS",
    "GRACE_MIN_SECONDS",
    "ONESHOT_GRACE_SECONDS",
    "SCHEMA_VERSION",
    "SILENT_MARKER",
    "ApprovalMode",
    "ConcurrencyPolicy",
    "DeliveryChannel",
    "DeliveryStatus",
    "DueTaskReservation",
    "MisfirePolicy",
    "RunFailureReason",
    "RunStatus",
    "ScheduleDelivery",
    "ScheduleTrigger",
    "ScheduledRun",
    "ScheduledRunRequest",
    "ScheduledTask",
    "SessionMode",
    "TaskExecutionContext",
    "TaskExecutionPolicy",
    "TaskOrigin",
    "TaskState",
    "TaskTarget",
    "TriggerType",
    "resolve_effective_mode",
]
