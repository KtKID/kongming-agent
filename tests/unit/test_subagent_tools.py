"""子 agent request 适配单元测试。

本脚本验证普通 ``spawn_subagent`` 参数和 workflow ``SubAgentTask`` 都能归一化为
``SpawnAgentRequest``。作用是固定 request 字段、默认值、错误路径和
``AgentSpec`` 构造规则，避免普通派生与 workflow 派生再次分叉。
关键执行流程：构造 tool 参数或 fake workflow task，调用 ``subagent_tools`` 适配函数，
断言输出 request / spec / seed_message / metadata 的稳定语义。
"""

from __future__ import annotations

import pytest

from application.agent_workflows.task_models import SubAgentTask
from application.agents.subagent_tools import (
    SpawnAgentRequest,
    build_child_agent_spec,
    build_spawn_request_from_tool_args,
    build_spawn_request_from_workflow_task,
    parent_agent_id_from_snapshot,
)
from application.subagents.runtime_resolver import ResolvedSubAgentRuntime
from core.agent_spec import AgentSpec
from core.message import Message


def _runtime() -> ResolvedSubAgentRuntime:
    """构造 workflow 适配测试 runtime，输入为空，输出已解析子 agent runtime。"""
    return ResolvedSubAgentRuntime(
        preset_id="child-preset",
        model="child-model",
        reasoning_effort="high",
        max_turns=4,
        max_tokens=2048,
        temperature=0.2,
        timeout_seconds=5.0,
        field_sources={},
        parent_agent={"model": "parent-model"},
        role_id="reviewer",
        role_nickname="Reviewer",
        role_description="审查实现边界",
    )


def test_spawn_agent_request_freezes_metadata_and_validates_required_fields() -> None:
    """SpawnAgentRequest 校验必填字段并冻结 metadata，输入为可变 dict，输出不可变 request。"""
    metadata = {"source": "unit"}
    request = SpawnAgentRequest(
        parent_agent_id=" root ",
        spec=AgentSpec(name="child", instructions="", default_model="m"),
        seed_message=Message.user("do it"),
        cwd=" /tmp/work ",
        metadata=metadata,
    )

    metadata["source"] = "mutated"
    assert request.parent_agent_id == "root"
    assert request.cwd == "/tmp/work"
    assert request.metadata["source"] == "unit"
    with pytest.raises(TypeError):
        request.metadata["new"] = "blocked"  # type: ignore[index]


def test_build_child_agent_spec_normalizes_defaults() -> None:
    """build_child_agent_spec 构造 AgentSpec，输入为可清洗字段，输出稳定默认值。"""
    spec = build_child_agent_spec(
        name=" child ",
        instructions=" instructions ",
        tool_names=(" read_file ", "", "write_file"),
        default_model=" model ",
        max_turns=None,
        metadata={"source_task_id": "task-1", "attempt": 2},
        reasoning_effort="high",
    )

    assert spec.name == "child"
    assert spec.instructions == "instructions"
    assert spec.default_model == "model"
    assert spec.tool_names == ("read_file", "write_file")
    assert spec.max_turns == 10
    assert spec.metadata["attempt"] == "2"
    assert spec.reasoning_effort == "high"


def test_spawn_request_from_tool_args_builds_request_and_spec() -> None:
    """普通 tool 参数转 request，输入为 prompt/name/cwd，输出统一 SpawnAgentRequest。"""
    request = build_spawn_request_from_tool_args(
        parent_agent_id="root-agent",
        source_task_id="call-1",
        parent_task_id="parent-task",
        prompt=" summarize ",
        name="summarizer",
        instructions="be concise",
        tool_names=("read_file",),
        cwd="/tmp/work",
        default_model="parent-model",
        max_turns=3,
        skill_names=("skill-a",),
        metadata={"trace": "x"},
    )

    assert request.parent_agent_id == "root-agent"
    assert request.source_task_id == "call-1"
    assert request.parent_task_id == "parent-task"
    assert request.seed_message.content == "summarize"
    assert request.cwd == "/tmp/work"
    assert request.skill_names == ("skill-a",)
    assert request.spec.name == "summarizer"
    assert request.spec.default_model == "parent-model"
    assert request.spec.tool_names == ("read_file",)
    assert request.requested_tool_names == ("read_file",)
    assert request.metadata["source"] == "spawn_subagent"


def test_spawn_request_distinguishes_missing_and_explicit_empty_tools() -> None:
    """普通 spawn 缺省工具表示继承父级，显式空 tuple 表示零工具。"""
    inherited = build_spawn_request_from_tool_args(
        parent_agent_id="root",
        prompt="inherit",
        name="child-inherit",
        cwd="/tmp",
        default_model="m",
        max_turns=1,
    )
    empty = build_spawn_request_from_tool_args(
        parent_agent_id="root",
        prompt="empty",
        name="child-empty",
        tool_names=(),
        cwd="/tmp",
        default_model="m",
        max_turns=1,
    )

    assert inherited.requested_tool_names is None
    assert empty.requested_tool_names == ()


def test_spawn_request_from_tool_args_rejects_empty_prompt() -> None:
    """普通 tool 参数错误路径，输入为空 prompt，输出 ValueError。"""
    with pytest.raises(ValueError, match="prompt"):
        build_spawn_request_from_tool_args(
            parent_agent_id="root",
            prompt=" ",
            name="child",
            cwd="/tmp",
            default_model="m",
            max_turns=1,
        )


def test_spawn_request_from_workflow_task_builds_request() -> None:
    """workflow task 转 request，输入为已解析 runtime 的 SubAgentTask，输出统一 request。"""
    task = SubAgentTask(
        task_id="task-a",
        task_name="Review API",
        prompt="检查 API",
        context="上下文",
        tool_names=("read_file",),
        skill_names=("skill-review",),
        agent_role_id="reviewer",
        runtime=_runtime(),
        metadata={"working_dir": "/tmp/w", "task_run_id": "001-task-a"},
    )

    request = build_spawn_request_from_workflow_task(
        parent_agent_id="root-agent",
        workflow_task=task,
        cwd="/tmp/w",
        parent_task_id="workflow-parent-task",
        metadata={"workflow_id": "wf-1", "attempt": 1},
    )

    assert request.parent_agent_id == "root-agent"
    assert request.source_task_id == "task-a"
    assert request.parent_task_id == "workflow-parent-task"
    assert request.cwd == "/tmp/w"
    assert request.role_id == "reviewer"
    assert request.skill_names == ("skill-review",)
    assert request.spec.default_model == "child-model"
    assert request.spec.max_turns == 4
    assert request.spec.reasoning_effort == "high"
    assert "检查 API" in (request.seed_message.content or "")
    assert "工作目录：" in (request.seed_message.content or "")
    assert "/tmp/w" in (request.seed_message.content or "")
    assert request.metadata["workflow_id"] == "wf-1"
    assert request.requested_tool_names == ("read_file",)


def test_workflow_spawn_distinguishes_missing_and_explicit_empty_tools() -> None:
    """workflow 缺省工具继承父级，显式空 requested tuple 保持零工具。"""
    inherited_task = SubAgentTask(
        task_id="inherit",
        task_name="inherit",
        prompt="inherit",
        runtime=_runtime(),
    )
    empty_task = SubAgentTask(
        task_id="empty",
        task_name="empty",
        prompt="empty",
        requested_tool_names=(),
        runtime=_runtime(),
    )

    inherited = build_spawn_request_from_workflow_task(
        parent_agent_id="root",
        workflow_task=inherited_task,
        cwd="/tmp",
    )
    empty = build_spawn_request_from_workflow_task(
        parent_agent_id="root",
        workflow_task=empty_task,
        cwd="/tmp",
    )

    assert inherited.requested_tool_names is None
    assert empty.requested_tool_names == ()


def test_spawn_request_from_workflow_task_requires_runtime() -> None:
    """workflow task 错误路径，输入为未解析 runtime 的任务，输出 ValueError。"""
    task = SubAgentTask(task_id="task-a", task_name="Task A", prompt="do it")
    with pytest.raises(ValueError, match="no resolved runtime"):
        build_spawn_request_from_workflow_task(
            parent_agent_id="root",
            workflow_task=task,
            cwd="/tmp",
        )


def test_parent_agent_id_from_snapshot_reads_optional_agent_id() -> None:
    """父 agent 快照解析，输入为 metadata 快照，输出 agent_id 或 None。"""
    assert parent_agent_id_from_snapshot({"agent_id": " root "}) == "root"
    assert parent_agent_id_from_snapshot({"agent_id": ""}) is None
    assert parent_agent_id_from_snapshot(None) is None
