"""agent role tool 单元测试。

本脚本验证 list_agent_roles / create_agent_role 的薄适配行为。
作用是确保 tool 层只读取 ToolContext.session_id 并调用 AgentRoleManager，业务回执
由 manager 生成。
关键执行流程：构造共享 manager 和两个 tool，执行 list/create，再断言 content 和 data。
关键函数：test_create_agent_role_tool_saves_and_returns_roster 覆盖创建工具闭环。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from application.agent_roles import AgentRoleManager
from core.contracts import ToolContext
from tools.agent_role_tool import CreateAgentRoleTool, ListAgentRolesTool


def _ctx(session_id: str = "s1") -> ToolContext:
    """构造 tool context，输入为 session id，输出 ToolContext。"""
    return ToolContext(run_id="r", session_id=session_id, turn=1, call_id="c")


@pytest.mark.asyncio
async def test_list_agent_roles_tool_returns_empty_hint(tmp_path: Path) -> None:
    """验证空角色库列表工具，输入为空目录，输出创建提示。"""
    tool = ListAgentRolesTool(AgentRoleManager(role_dir=tmp_path / "roles"))

    result = await tool.execute({}, _ctx())

    assert result.ok is True
    assert result.data == {
        "roles": [],
        "current_roundtable_agents": [],
        "empty_message": "No agent roles are available. Call create_agent_role with id, title, and role.",
    }
    assert "create_agent_role" in result.content


@pytest.mark.asyncio
async def test_create_agent_role_tool_saves_and_returns_roster(tmp_path: Path) -> None:
    """验证创建工具，输入为三字段，输出保存结果和 roster。"""
    manager = AgentRoleManager(role_dir=tmp_path / "roles")
    tool = CreateAgentRoleTool(manager)

    result = await tool.execute(
        {"id": "risk_skeptic", "title": "风险质询者", "role": "寻找隐藏风险"},
        _ctx("s1"),
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["status"] == "created"
    assert result.data["role"] == {
        "id": "risk_skeptic",
        "title": "风险质询者",
        "role": "寻找隐藏风险",
    }
    assert result.data["current_roundtable_agents"] == [result.data["role"]]
    assert "agent role saved: risk_skeptic" in result.content


@pytest.mark.asyncio
async def test_create_agent_role_tool_passes_session_id_to_manager(tmp_path: Path) -> None:
    """验证 tool 传递 session_id，输入为两个 session，输出两个独立 roster。"""
    manager = AgentRoleManager(role_dir=tmp_path / "roles")
    create = CreateAgentRoleTool(manager)
    list_tool = ListAgentRolesTool(manager)

    await create.execute({"id": "a", "title": "A", "role": "角色 A"}, _ctx("s1"))
    await create.execute({"id": "b", "title": "B", "role": "角色 B"}, _ctx("s2"))
    s1 = await list_tool.execute({}, _ctx("s1"))
    s2 = await list_tool.execute({}, _ctx("s2"))

    assert s1.data is not None
    assert s2.data is not None
    assert [item["id"] for item in s1.data["current_roundtable_agents"]] == ["a"]
    assert [item["id"] for item in s2.data["current_roundtable_agents"]] == ["b"]


@pytest.mark.asyncio
async def test_create_agent_role_tool_rejects_non_string_fields(tmp_path: Path) -> None:
    """验证工具不改写非字符串参数，输入为数字 id，输出 manager 校验错误。"""
    tool = CreateAgentRoleTool(AgentRoleManager(role_dir=tmp_path / "roles"))

    result = await tool.execute(
        {"id": 123, "title": "风险质询者", "role": "寻找隐藏风险"},
        _ctx("s1"),
    )

    assert result.ok is False
    assert result.error_message == "id must be a string"
