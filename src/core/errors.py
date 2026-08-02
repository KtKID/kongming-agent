"""核心错误模型。

core 在 v1-mini 阶段只区分必要的失败类别，避免过早把错误形状收死。
后续 safety / provider / approval 细分异常时，继承对应基类即可。
"""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """所有 agent 内部错误的基类。

    ``details`` 里可以携带结构化信息（例如 provider 原始错误、call_id 等），
    以便 infrastructure.tracing / host 层做进一步展示或落盘，而无需依赖 str(exc) 解析。
    """

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def __repr__(self) -> str:  # pragma: no cover - 便于日志输出
        return f"{type(self).__name__}({self.message!r}, details={self.details!r})"


class ProviderError(AgentError):
    """LLM provider 调用失败。

    对应 ``executors/llm/*`` 在网络、鉴权、解析、超时等场景下抛出的错误。
    """


class LLMToolCallContractError(AgentError):
    """LLM assistant 响应违反显式工具调用合同。"""


class ToolError(AgentError):
    """工具执行失败。

    工具实现内部抛出的异常由 runner 包成 ToolError 回填，避免裸异常中断主链路。
    """


class ToolPreparationError(ToolError):
    """工具在审批前无法生成稳定 prepared call。"""


class ApprovalRejected(AgentError):
    """审批被拒绝。

    由 :class:`core.contracts.ApprovalProvider` 返回 ``approved=False`` 时触发。
    具体是否立即终止 run，由装配层或调用方决定。
    """


class CapabilityDenied(AgentError):
    """粗粒度能力门禁拒绝。

    预留给 ``safety/capability_policy.py``，core 本身不会直接抛出，
    但声明在这里以保证安全链路的错误类型统一。
    """


class PermissionDenied(AgentError):
    """细粒度权限规则拒绝。

    预留给 ``safety/permission_policy.py``，同样由装配层驱动抛出。
    """


class MaxTurnsExceededError(AgentError):
    """runner 达到 ``max_turns`` 仍未收敛。

    抛出前 runner 会把当前已有的消息保留在 session 中，便于后续恢复或诊断。
    """


class ConfigError(AgentError):
    """配置缺失或不合法。

    预留给统一配置层，core 只声明类型；实际抛出方通常是装配层或 provider 构造函数。
    """


__all__ = [
    "AgentError",
    "ApprovalRejected",
    "CapabilityDenied",
    "ConfigError",
    "LLMToolCallContractError",
    "MaxTurnsExceededError",
    "PermissionDenied",
    "ProviderError",
    "ToolError",
    "ToolPreparationError",
]
