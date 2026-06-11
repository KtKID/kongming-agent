"""agent role manager 单元测试。

本脚本验证 AgentRoleManager 的角色读写、result types、session roster、participant
解析和 workflow 快照。
作用是把 agent-role-presets-v0.1 的应用层合同固定为可重复测试，避免 tool 或
roundtable strategy 自行拼装角色状态。
关键执行流程：构造临时角色目录，调用 manager 的 list/create/resolve/snapshot 方法，
断言返回结构、错误语义和文件产物。
关键函数：test_create_role_saves_json_and_returns_roster 覆盖创建闭环，
test_resolve_participants_rejects_unknown_role_id 覆盖失败路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from application.agent_roles import AgentRoleManager, AgentRolePreset


def _manager(tmp_path: Path) -> AgentRoleManager:
    """构造测试 manager，输入为 tmp_path，输出带一个内置角色的 manager。"""
    return AgentRoleManager(
        role_dir=tmp_path / "roles",
        builtin_roles=(
            AgentRolePreset(
                role_id="builtin_architect",
                title="内置架构师",
                role="审查架构边界",
            ),
        ),
    )


def test_agent_role_manager_lists_empty_roles_with_create_hint(tmp_path: Path) -> None:
    """验证空用户角色库，输入为无内置 manager，输出空列表和创建提示。"""
    manager = AgentRoleManager(role_dir=tmp_path / "roles")

    result = manager.list_role_summaries(session_id="s1")

    assert result.roles == []
    assert result.current_roundtable_agents == []
    assert result.empty_message


def test_create_role_saves_json_and_returns_roster(tmp_path: Path) -> None:
    """验证创建角色，输入为三字段，输出 JSON 文件和 session roster。"""
    manager = _manager(tmp_path)

    result = manager.create_role(
        session_id="s1",
        role_id="risk_skeptic",
        title="风险质询者",
        role="寻找隐藏风险",
    )

    assert result.status == "created"
    assert result.role == {"id": "risk_skeptic", "title": "风险质询者", "role": "寻找隐藏风险"}
    assert result.current_roundtable_agents == [result.role]
    role_file = tmp_path / "roles" / "risk_skeptic.json"
    assert json.loads(role_file.read_text(encoding="utf-8")) == result.role


def test_create_role_existing_returns_existing_and_dedupes_roster(tmp_path: Path) -> None:
    """验证重复创建，输入为同一 role id，输出 existing 且 roster 不重复。"""
    manager = _manager(tmp_path)

    first = manager.create_role(
        session_id="s1", role_id="risk_skeptic", title="风险质询者", role="寻找隐藏风险"
    )
    second = manager.create_role(
        session_id="s1", role_id="risk_skeptic", title="风险质询者", role="寻找隐藏风险"
    )

    assert first.status == "created"
    assert second.status == "existing"
    assert second.current_roundtable_agents == [first.role]


def test_agent_role_manager_roster_is_session_scoped(tmp_path: Path) -> None:
    """验证 roster 按 session 隔离，输入为两个 session，输出互不串扰。"""
    manager = _manager(tmp_path)

    manager.create_role(session_id="s1", role_id="role_a", title="A", role="角色 A")
    manager.create_role(session_id="s2", role_id="role_b", title="B", role="角色 B")

    s1 = manager.list_role_summaries(session_id="s1")
    s2 = manager.list_role_summaries(session_id="s2")
    assert [item["id"] for item in s1.current_roundtable_agents] == ["role_a"]
    assert [item["id"] for item in s2.current_roundtable_agents] == ["role_b"]


def test_agent_role_manager_skips_corrupt_role_file(tmp_path: Path) -> None:
    """验证损坏 JSON 被跳过，输入为坏文件和好文件，输出可读角色。"""
    role_dir = tmp_path / "roles"
    role_dir.mkdir()
    (role_dir / "bad.json").write_text("{", encoding="utf-8")
    (role_dir / "good.json").write_text(
        json.dumps({"id": "good", "title": "好角色", "role": "可读取"}, ensure_ascii=False),
        encoding="utf-8",
    )
    manager = AgentRoleManager(role_dir=role_dir)

    assert [role.role_id for role in manager.list_roles()] == ["good"]


@pytest.mark.parametrize("role_id", ["", "../bad", "bad/path", "x" * 65])
def test_agent_role_manager_rejects_invalid_role_id(tmp_path: Path, role_id: str) -> None:
    """验证非法 role id 被拒绝，输入为坏 id，输出 ValueError。"""
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="invalid role id"):
        manager.create_role(session_id="s1", role_id=role_id, title="T", role="R")


@pytest.mark.parametrize(
    ("title", "role", "match"),
    [
        ("", "R", "title is required"),
        ("T", "", "role is required"),
        ("T", "x" * 1201, "role is too long"),
    ],
)
def test_agent_role_manager_rejects_invalid_text(
    tmp_path: Path,
    title: str,
    role: str,
    match: str,
) -> None:
    """验证 title/role 文本校验，输入为非法文本，输出 ValueError。"""
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match=match):
        manager.create_role(session_id="s1", role_id="role_a", title=title, role=role)


def test_resolve_participants_dedupes_and_preserves_order(tmp_path: Path) -> None:
    """验证 participant 解析，输入为重复 ids，输出去重保序角色。"""
    manager = _manager(tmp_path)
    manager.create_role(session_id="s1", role_id="risk_skeptic", title="风险", role="找风险")

    roles = manager.resolve_participants(["risk_skeptic", "builtin_architect", "risk_skeptic"])

    assert [role.role_id for role in roles] == ["risk_skeptic", "builtin_architect"]


def test_resolve_participants_rejects_unknown_role_id(tmp_path: Path) -> None:
    """验证未知角色失败，输入为不存在 id，输出 ValueError。"""
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="unknown role id: missing"):
        manager.resolve_participants(["missing"])


def test_write_workflow_snapshot(tmp_path: Path) -> None:
    """验证 workflow 快照，输入为角色列表，输出 roles.json。"""
    manager = _manager(tmp_path)
    roles = manager.resolve_participants(["builtin_architect"])

    path = manager.write_workflow_snapshot(tmp_path / "workflow", roles)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "source": "agent_role_manager",
        "roles": [{"id": "builtin_architect", "title": "内置架构师", "role": "审查架构边界"}],
    }
