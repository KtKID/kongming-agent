"""Tool runtime protocols and value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Tool 相关支撑类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolContext:
    """工具执行上下文。

    Attributes:
        run_id: 当前 run 的 id，便于工具在日志 / trace 里标识来源。
        session_id: 当前 session id。
        turn: 工具被调用所在的 turn，从 1 开始计数。
        call_id: 对应的 :class:`core.message.ToolCall.call_id`。
        metadata: 装配层注入的额外上下文（例如 cwd、env 快照），core 不解释内容。
        agent_id: 调用该工具的 agent id（agent-tree-v0.1 模块 G）。单 agent
            场景默认 ``""``，由 runner 在 ToolContext 构建处填入真实值；工具
            实现可读 ``ctx.agent_id`` 判断被哪个 agent 调用（不加 epoch）。
    """

    run_id: str
    session_id: str
    turn: int
    call_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    agent_id: str = ""


@dataclass(frozen=True)
class ToolExecutionScope:
    """工具准备阶段冻结的实际执行边界。

    ``cwd`` 表示工具真正执行时使用的 canonical absolute working directory。
    没有目录语义的工具保留 ``None``。
    """

    cwd: str | None = None


@dataclass(frozen=True)
class PreparedToolCall:
    """审批与执行共同消费的工具调用快照。"""

    arguments: dict[str, Any]
    execution_scope: ToolExecutionScope = field(default_factory=ToolExecutionScope)


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。

    ``ok=False`` 表示工具自己判定失败但已有结构化信息。
    工具实现抛出的原始异常由 runner 在外层包成
    :class:`core.errors.ToolError`，不走这个字段。
    """

    ok: bool
    content: str
    data: dict[str, Any] | None = None
    error_message: str | None = None


@runtime_checkable
class Tool(Protocol):
    """统一工具协议。

    实现方通常在 ``tools/`` 下面。core 本身不提供 Tool 实现。

    Attributes:
        name: 唯一工具名，模型通过它在 tool_call 里指向具体工具。
        description: 自然语言描述，会出现在 LLM 的 tools 列表里。
        input_schema: 参数 JSON schema；provider 适配层负责转换成目标格式。
    """

    name: str
    description: str
    input_schema: dict[str, Any]

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        """执行一次已准备的工具调用。必须是 async。

        调用方必须在审批前完成参数校验、默认填充与语义归一化，并把冻结事实
        放入 :class:`PreparedToolCall`。执行入口只消费该快照。
        """
        ...


@runtime_checkable
class ToolCallPreparer(Protocol):
    """可在审批前把模型参数解析为稳定执行事实的工具合同。"""

    def prepare(self, arguments: dict[str, Any], context: ToolContext) -> PreparedToolCall:
        """纯函数、幂等地返回审批与执行共用的 prepared call。"""
        ...


@runtime_checkable
class ToolLookup(Protocol):
    """工具查找面。

    runner 只依赖这一层，不依赖具体 ``tools/runtime/registry.py``。
    ``Mapping[str, Tool]`` 天然满足此 Protocol（因为 ``__getitem__`` 和
    ``__contains__`` 都在），所以测试里可以直接传 dict。
    """

    def __contains__(self, name: object) -> bool: ...
    def __getitem__(self, name: str) -> Tool: ...


# ---------------------------------------------------------------------------
# Approval 相关支撑类型
__all__ = [
    "PreparedToolCall",
    "Tool",
    "ToolCallPreparer",
    "ToolContext",
    "ToolExecutionScope",
    "ToolLookup",
    "ToolResult",
]
