"""Agent role 工具适配层。

本脚本负责把 LLM tool call 转换为 AgentRoleManager 的 list/create 请求。
作用是向主 agent 暴露 list_agent_roles 与 create_agent_role，同时保持角色创建、
roster、持久化和快照逻辑都归应用层 manager 管理。
关键执行流程：tool 从 ToolContext 读取 session_id，校验 LLM 参数，调用 manager，
再把 manager 生成的 result 原样投影为 ToolResult content/data。
关键函数：ListAgentRolesTool._run 列出角色，CreateAgentRoleTool._run 创建或复用角色，
build_agent_role_tools 构造注册所需工具列表。
"""

from __future__ import annotations

from typing import Any, Protocol

from core.contracts import PreparedToolCall, ToolContext
from tools.runtime.base import BaseBuiltinTool


class AgentRoleListResultLike(Protocol):
    """角色列表结果协议，输入为 manager 返回对象，输出供 tool 格式化。"""

    @property
    def roles(self) -> list[dict[str, object]]:
        """返回可选角色列表，输入为空，输出 agent.toml 字段。"""
        ...

    @property
    def current_roundtable_agents(self) -> list[dict[str, object]]:
        """返回当前 session roster，输入为空，输出 agent.toml 字段。"""
        ...

    @property
    def empty_message(self) -> str | None:
        """返回空角色库提示，输入为空，输出提示文本或 None。"""
        ...

    def to_data(self) -> dict[str, object]:
        """转换为 tool data，输入为结果对象，输出 JSON 友好 dict。"""
        ...


class AgentRoleCreateResultLike(Protocol):
    """角色创建结果协议，输入为 manager 返回对象，输出供 tool 格式化。"""

    @property
    def status(self) -> str:
        """返回创建状态，输入为空，输出 created/existing。"""
        ...

    @property
    def role(self) -> dict[str, object]:
        """返回角色摘要，输入为空，输出 agent.toml 字段。"""
        ...

    @property
    def path(self) -> str:
        """返回保存路径，输入为空，输出路径文本。"""
        ...

    @property
    def current_roundtable_agents(self) -> list[dict[str, object]]:
        """返回当前 session roster，输入为空，输出 agent.toml 字段。"""
        ...

    def to_data(self) -> dict[str, object]:
        """转换为 tool data，输入为结果对象，输出 JSON 友好 dict。"""
        ...


class AgentRoleManagerLike(Protocol):
    """角色 manager 协议，输入为工具调用，输出 list/create 结果。"""

    def list_role_summaries(self, *, session_id: str | None = None) -> AgentRoleListResultLike:
        """列出角色摘要，输入为 session id，输出 LLM 可见角色列表。"""
        ...

    def create_role(
        self,
        *,
        session_id: str,
        role_id: str,
        title: str,
        role: str,
    ) -> AgentRoleCreateResultLike:
        """创建角色，输入为 session 和三字段，输出创建回执。"""
        ...


class ListAgentRolesTool(BaseBuiltinTool):
    """列出可用 agent 角色，输入为空，输出角色摘要和当前 roster。"""

    name = "list_agent_roles"
    description = (
        "List reusable sub-agent roles before starting a roundtable or multi-agent workflow. "
        "Returns id, nickname, model, role_desc, reasoning_effort, max_turns, "
        "plus current_roundtable_agents for this session. "
        "If no suitable role exists, call create_agent_role with id, title, and role."
    )
    input_schema: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, manager: AgentRoleManagerLike) -> None:
        """初始化工具，输入为共享 AgentRoleManager，输出为可执行工具实例。"""
        self._manager = manager

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """执行角色列表查询，输入为 tool 参数和上下文，输出文本和 data。"""
        result = self._manager.list_role_summaries(session_id=ctx.session_id)
        return _format_list_result(result), result.to_data()


class CreateAgentRoleTool(BaseBuiltinTool):
    """创建或复用 agent 角色，输入为 id/title/role，输出创建回执和 roster。"""

    name = "create_agent_role"
    description = (
        "Create a reusable sub-agent role when list_agent_roles has no suitable role. "
        "Only provide id, title, and role. The role is saved under .kongming/agent_roles "
        "and added to current_roundtable_agents for this session."
    )
    input_schema: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Stable role id, using letters, numbers, underscore or hyphen.",
            },
            "title": {"type": "string", "description": "Short display title."},
            "role": {"type": "string", "description": "Role responsibility and perspective."},
        },
        "required": ["id", "title", "role"],
    }

    def __init__(self, manager: AgentRoleManagerLike) -> None:
        """初始化工具，输入为共享 AgentRoleManager，输出为可执行工具实例。"""
        self._manager = manager

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前校验并冻结角色创建参数。"""
        del context
        self._validate_args(arguments)
        return PreparedToolCall(
            arguments={
                "id": _required_string_arg(arguments, "id"),
                "title": _required_string_arg(arguments, "title"),
                "role": _required_string_arg(arguments, "role"),
            }
        )

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """执行角色创建，输入为 tool 参数和上下文，输出文本和 data。"""
        result = self._manager.create_role(
            session_id=ctx.session_id,
            role_id=args["id"],
            title=args["title"],
            role=args["role"],
        )
        return _format_create_result(result), result.to_data()


def build_agent_role_tools(manager: AgentRoleManagerLike) -> tuple[BaseBuiltinTool, ...]:
    """构造角色工具列表，输入为共享 manager，输出为两个 tool 实例。"""
    return (ListAgentRolesTool(manager), CreateAgentRoleTool(manager))


def _format_list_result(result: AgentRoleListResultLike) -> str:
    """格式化列表结果，输入为 manager result，输出给 LLM 的文本。"""
    lines = ["agent roles:"]
    if not result.roles:
        lines.append("- none")
    for role in result.roles:
        lines.append(
            f"- {role['id']} | {role['nickname']} | {role['model']} | "
            f"{role['reasoning_effort']} | max_turns={role['max_turns']} | {role['role_desc']}"
        )
    if result.empty_message:
        lines.extend(["", result.empty_message])
    lines.extend(["", "current_roundtable_agents:"])
    if not result.current_roundtable_agents:
        lines.append("- none")
    for role in result.current_roundtable_agents:
        lines.append(
            f"- {role['id']} | {role['nickname']} | {role['model']} | "
            f"{role['reasoning_effort']} | max_turns={role['max_turns']} | {role['role_desc']}"
        )
    return "\n".join(lines)


def _format_create_result(result: AgentRoleCreateResultLike) -> str:
    """格式化创建结果，输入为 manager result，输出给 LLM 的文本。"""
    lines = [
        f"agent role saved: {result.role['id']}",
        f"status: {result.status}",
        f"path: {result.path}",
        "",
        "current_roundtable_agents:",
    ]
    for role in result.current_roundtable_agents:
        lines.append(
            f"- {role['id']} | {role['nickname']} | {role['model']} | "
            f"{role['reasoning_effort']} | max_turns={role['max_turns']} | {role['role_desc']}"
        )
    return "\n".join(lines)


def _required_string_arg(args: dict[str, Any], key: str) -> str:
    """读取必填字符串参数，输入为 tool 参数和字段名，输出字符串或抛错。"""
    value = args.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


__all__ = [
    "AgentRoleManagerLike",
    "CreateAgentRoleTool",
    "ListAgentRolesTool",
    "build_agent_role_tools",
]
