"""Approval protocols and value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from core.contracts.tool_runtime import ToolExecutionScope

ApprovalOutcome = Literal["approved", "rejected", "cancelled", "pending"]


@dataclass(frozen=True)
class ApprovalRequest:
    """一次审批请求。

    Attributes:
        run_id / session_id / turn / call_id: 定位该次调用的运行坐标。
        tool_name: 待审批的工具名。
        arguments: preparation 后冻结的参数。审批端可以据此展示给人看。
        execution_scope: preparation 后冻结的实际执行边界。
        reason: 装配层给出的补充理由（例如"命中 permission=ask 规则"）。
        metadata: 额外信息，例如涉及文件路径、执行命令等摘要。
    """

    run_id: str
    session_id: str
    turn: int
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    execution_scope: ToolExecutionScope = field(default_factory=ToolExecutionScope)
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalDecision:
    """一次审批结果。

    Attributes:
        outcome: approved / rejected / cancelled / pending 之一。
                 ``pending`` 仅用于 trace 占位（如 destructive_force_ask / approval_required），
                 不参与控制流决策。
        reason: 人工或策略给出的理由文本。
        metadata: 附加信息，比如操作者身份、来源 UI 等。

    **safety v0.1.4 metadata 字段约定**（不改变协议形态，仅约定 metadata 内的键名）：

    - ``decision_class``: ``hard_block`` / ``silent_allow`` / ``explicit_consent``
      —— 主决策三段式分类。
    - ``decision_source``: ``intrinsic`` / ``session`` / ``config``
      （silent_allow 用） / ``standard`` / ``elevated``（explicit_consent 用）
      —— 证据来源或审批强度。``hard_block`` 不使用此字段。
    - ``matched_rule``: 命中规则的字符串描述（如 ``"deny:~/.ssh/"``、
      ``"grant:tests/integration/"``）。
    - ``reason``: 人读理由（用于 trace / UI 展示）。
    - ``remember``: 人工审批是否把规则写入当前 thread permissions。
    - ``boundary_kind``: ``host`` / ``sandbox`` —— v0.1.4 实现恒为 ``host``，
      为未来 sandbox 落地预留。
    - ``suggested_alternatives``: ``list[str]`` —— 仅 hard_block 时携带，
      给 LLM 的备选建议（如 "改写到 ~/scratch/ 后再要"）。
    - ``stage``: 兼容 v0.1.3 字段（``capability`` / ``permission`` / ``approval``），
      由 SafetyDecisionEngine 按映射规则填充。

    详见 ``docs/safety-scope-v0.1.4/04-data-and-state.md``。
    """

    outcome: ApprovalOutcome
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        """便捷判断：只有 ``outcome == "approved"`` 才视为通过。"""
        return self.outcome == "approved"


@runtime_checkable
class ApprovalProvider(Protocol):
    """审批入口协议。

    第一批默认实现是 ``tools/runtime/approval.py`` 里的 ``InteractiveApproval``；
    后续 safety 模块会基于策略返回决定。核心约束：
    **不改变协议形状，只新增实现**。
    """

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """对一次工具调用做出审批决定。"""
        ...


@runtime_checkable
class InteractiveApprovalRebinder(Protocol):
    """保留安全决策链，仅替换最终人工审批 Provider 的门户能力。"""

    def with_interactive_approval(
        self,
        interactive_approval: ApprovalProvider,
    ) -> ApprovalProvider:
        """返回复用原安全策略、绑定新人工审批终点的 ApprovalProvider。"""
        ...


class ApprovalAction(StrEnum):
    """v0.1.4 安全链审批结果的结构化 action。

    ``InteractiveApproval`` 在 v0.1.3 仅返回 ``bool``；v0.1.4 升级为携带
    ``ApprovalAction`` 的结构化决策，让用户除了"允许 / 拒绝"外，还能
    选择"本会话允许"或"持久化到配置"。

    - ``ACCEPT_ONCE``：一次性允许，不缓存 grant
    - ``ACCEPT_FOR_SESSION``：旧命名，当前语义为允许并记住到 thread permissions
    - ``ACCEPT_PERSIST``：旧命名，当前同样映射为允许并记住
    - ``REJECT``：拒绝

    映射到 :class:`ApprovalDecision`：

    - ``ACCEPT_ONCE`` → ``outcome="approved"``，``metadata.grant_scope`` 缺省
    - ``ACCEPT_FOR_SESSION`` / ``ACCEPT_PERSIST`` → ``outcome="approved"``，
      ``metadata.remember=true``
    - ``REJECT`` → ``outcome="rejected"``

    向后兼容：v0.1.3 旧 bool 返回路径由 adapter 自动映射为
    ``ACCEPT_ONCE`` / ``REJECT``，不破坏现有 ApprovalProvider 实现。
    """

    ACCEPT_ONCE = "accept_once"
    ACCEPT_FOR_SESSION = "accept_for_session"
    ACCEPT_PERSIST = "accept_persist"
    REJECT = "reject"


# ---------------------------------------------------------------------------
# LLM Provider 相关支撑类型
# ---------------------------------------------------------------------------
__all__ = [
    "ApprovalAction",
    "ApprovalDecision",
    "ApprovalOutcome",
    "ApprovalProvider",
    "ApprovalRequest",
    "InteractiveApprovalRebinder",
]
