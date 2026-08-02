"""子 agent 派生工具函数集合。

本脚本负责把普通 ``spawn_subagent`` tool 参数和 workflow ``SubAgentTask`` 参数
归一化成同一个 ``SpawnAgentRequest``，并集中构造子 ``AgentSpec``。
关键执行流程：外部入口先校验各自参数，再调用本模块适配函数生成 request，最后交给
``AgentManager.spawn(request)`` 完成创建、登记和 child ``agent_loop`` 启动。
关键函数：``build_child_agent_spec`` 构造子 agent 静态规格，
``build_spawn_request_from_tool_args`` 适配普通派生，
``build_spawn_request_from_workflow_task`` 适配 workflow 子任务。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from application.agent_workflows.task_models import SubAgentTask
from core.agent_spec import AgentSpec, coerce_reasoning_effort
from core.contracts import ReasoningEffort, Tool
from core.lifecycle import LifecycleHook
from core.message import Message

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class SpawnAgentRequest:
    """子 agent 创建的唯一内部合同。

    字段创建后保持稳定值对象语义。``source_task_id`` 只用于关联外部来源，
    ``parent_task_id`` 传给 ``TaskRegistry`` 形成运行账本父链；新子 run 的
    registry task id 仍由 ``TaskRegistry`` 生成并通过 ``SpawnResult.task_id`` 返回。
    """

    parent_agent_id: str
    spec: AgentSpec
    seed_message: Message
    cwd: str
    child_session_id: str | None = None
    source_task_id: str | None = None
    parent_task_id: str | None = None
    role_id: str | None = None
    skill_names: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    requested_tool_names: tuple[str, ...] | None = None
    scope_allowed_tool_names: tuple[str, ...] | None = None
    enabled_tools: tuple[Tool, ...] | None = None
    lifecycle_hooks: tuple[LifecycleHook, ...] = ()
    max_tokens: int | None = None
    temperature: float | None = None
    timeout_seconds: float | None = None
    llm_request_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结 request 的可变集合字段，输入为 dataclass 初值，输出为规范化后的字段。"""
        parent_agent_id = self.parent_agent_id.strip()
        cwd = self.cwd.strip()
        if not parent_agent_id:
            raise ValueError("parent_agent_id must be a non-empty string")
        if not cwd:
            raise ValueError("cwd must be a non-empty string")
        object.__setattr__(self, "parent_agent_id", parent_agent_id)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "skill_names", tuple(self.skill_names))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if self.requested_tool_names is not None:
            object.__setattr__(
                self,
                "requested_tool_names",
                _normalize_string_tuple(self.requested_tool_names),
            )
        if self.scope_allowed_tool_names is not None:
            object.__setattr__(
                self,
                "scope_allowed_tool_names",
                _normalize_string_tuple(self.scope_allowed_tool_names),
            )
        if self.enabled_tools is not None:
            object.__setattr__(self, "enabled_tools", tuple(self.enabled_tools))
        object.__setattr__(self, "lifecycle_hooks", tuple(self.lifecycle_hooks))
        object.__setattr__(
            self,
            "llm_request_metadata",
            MappingProxyType(dict(self.llm_request_metadata)),
        )


def build_child_agent_spec(
    *,
    name: str,
    instructions: str,
    tool_names: tuple[str, ...],
    default_model: str,
    max_turns: int | None,
    metadata: Mapping[str, Any] | None = None,
    reasoning_effort: str | None = None,
) -> AgentSpec:
    """构造子 AgentSpec，输入为规格字段，输出可交给 AgentManager.spawn 的 AgentSpec。"""
    name_value = _require_text(name, "name")
    model_value = _require_text(default_model, "default_model")
    resolved_max_turns = max_turns if isinstance(max_turns, int) and max_turns > 0 else 10
    return AgentSpec(
        name=name_value,
        instructions=instructions.strip(),
        default_model=model_value,
        tool_names=_normalize_string_tuple(tool_names),
        max_turns=resolved_max_turns,
        metadata=_string_metadata(metadata or {}),
        reasoning_effort=_coerce_reasoning(reasoning_effort),
    )


def build_spawn_request_from_tool_args(
    *,
    parent_agent_id: str,
    source_task_id: str | None = None,
    parent_task_id: str | None = None,
    prompt: str,
    name: str,
    instructions: str = "",
    tool_names: tuple[str, ...] | None = None,
    cwd: str,
    default_model: str,
    max_turns: int | None,
    role_id: str | None = None,
    skill_names: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
    reasoning_effort: str | None = None,
) -> SpawnAgentRequest:
    """适配普通派生参数，输入为 tool 参数和父上下文，输出 SpawnAgentRequest。"""
    prompt_value = _require_text(prompt, "prompt")
    request_metadata = {
        "source": "spawn_subagent",
        **dict(metadata or {}),
    }
    if source_task_id:
        request_metadata["source_task_id"] = source_task_id
    spec = build_child_agent_spec(
        name=name,
        instructions=instructions,
        tool_names=tool_names or (),
        default_model=default_model,
        max_turns=max_turns,
        metadata={
            "agent_role": "subagent",
            "spawn_source": "tool",
            **_string_metadata(request_metadata),
        },
        reasoning_effort=reasoning_effort,
    )
    return SpawnAgentRequest(
        parent_agent_id=parent_agent_id,
        spec=spec,
        seed_message=Message.user(prompt_value),
        cwd=cwd,
        source_task_id=_optional_text(source_task_id),
        parent_task_id=_optional_text(parent_task_id),
        role_id=_optional_text(role_id),
        skill_names=_normalize_string_tuple(skill_names),
        metadata=request_metadata,
        requested_tool_names=tool_names,
    )


def build_spawn_request_from_workflow_task(
    *,
    parent_agent_id: str,
    workflow_task: SubAgentTask,
    cwd: str,
    child_session_id: str | None = None,
    parent_task_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    enabled_tools: tuple[Tool, ...] | None = None,
    scope_allowed_tool_names: tuple[str, ...] | None = None,
    lifecycle_hooks: tuple[LifecycleHook, ...] = (),
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout_seconds: float | None = None,
    llm_request_metadata: Mapping[str, Any] | None = None,
) -> SpawnAgentRequest:
    """适配 workflow 子任务，输入为 SubAgentTask 和 workflow 上下文，输出 SpawnAgentRequest。"""
    if not workflow_task.task_id.strip():
        raise ValueError("workflow task_id must be a non-empty string")
    if not workflow_task.prompt.strip():
        raise ValueError("workflow task prompt must be a non-empty string")
    runtime = workflow_task.runtime
    if runtime is None:
        raise ValueError(f"subagent task {workflow_task.task_id!r} has no resolved runtime")

    role_instructions = ""
    if runtime.role_description:
        role_instructions = f"你的角色职责：{runtime.role_description}。"
    request_metadata = {
        "source": "workflow",
        "workflow_task_id": workflow_task.task_id,
        "task_name": workflow_task.task_name,
        **dict(metadata or {}),
    }
    spec = build_child_agent_spec(
        name=f"subagent-{_slug(workflow_task.task_name)}",
        instructions=(
            "你是 kongming 子 agent。只处理分派给你的任务。"
            "只使用本次派发的任务文本和必要上下文。"
            "如果任务给出工作目录，文件写入必须位于该目录内。"
            "任务要求输出结论或报告时，直接作为最终回复返回。"
            "只有任务明确要求写文件且提供写入工具时才写文件。"
            "输出包含：结论、关键依据、风险或未完成项。"
            f"{role_instructions}"
        ),
        tool_names=tuple(workflow_task.tool_names),
        default_model=runtime.model,
        max_turns=runtime.max_turns,
        metadata={
            "agent_role": "subagent",
            "spawn_source": "workflow",
            "subagent_model_name": runtime.model,
            "model_preset_id": runtime.preset_id,
            "subagent_role_id": runtime.role_id or "",
            **_string_metadata(request_metadata),
        },
        reasoning_effort=runtime.reasoning_effort,
    )
    return SpawnAgentRequest(
        parent_agent_id=parent_agent_id,
        spec=spec,
        seed_message=Message.user(_workflow_seed_text(workflow_task)),
        cwd=cwd,
        child_session_id=_optional_text(child_session_id),
        source_task_id=workflow_task.task_id,
        parent_task_id=_optional_text(parent_task_id),
        role_id=_optional_text(runtime.role_id or workflow_task.agent_role_id),
        skill_names=_normalize_string_tuple(workflow_task.skill_names),
        metadata=request_metadata,
        requested_tool_names=workflow_task.requested_tool_names,
        scope_allowed_tool_names=scope_allowed_tool_names,
        enabled_tools=enabled_tools,
        lifecycle_hooks=lifecycle_hooks,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        llm_request_metadata=llm_request_metadata or {},
    )


def parent_agent_id_from_snapshot(parent_agent: Mapping[str, object] | None) -> str | None:
    """读取父 agent id，输入为 runner 透传快照，输出 agent_id 或 None。"""
    if parent_agent is None:
        return None
    raw = parent_agent.get("agent_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _workflow_seed_text(task: SubAgentTask) -> str:
    """渲染 workflow 子任务首条用户消息，输入为 SubAgentTask，输出 prompt 文本。"""
    parts = [
        f"任务名称：{task.task_name}",
        "",
        "任务：",
        task.prompt.strip(),
    ]
    if task.context.strip():
        parts.extend(["", "必要上下文：", task.context.strip()])
    working_dir = task.metadata.get("working_dir")
    if isinstance(working_dir, str) and working_dir.strip():
        parts.extend(["", "工作目录：", working_dir.strip()])
        if task.tool_names:
            parts.append("所有文件写入都必须在这个工作目录内。")
    return "\n".join(parts).strip()


def _require_text(value: str, field_name: str) -> str:
    """校验必填字符串，输入为原值和字段名，输出去空白文本。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: str | None) -> str | None:
    """规范化可选字符串，输入为字符串或 None，输出去空白字符串或 None。"""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_string_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    """过滤字符串元组，输入为任意字符串元组，输出去空白后的稳定元组。"""
    return tuple(value.strip() for value in values if isinstance(value, str) and value.strip())


def _string_metadata(metadata: Mapping[str, Any]) -> dict[str, str]:
    """把 metadata 压成 AgentSpec 需要的字符串字典，输入为任意 mapping，输出 str 字典。"""
    return {
        str(key): str(value)
        for key, value in metadata.items()
        if isinstance(key, str) and value is not None
    }


def _coerce_reasoning(value: str | None) -> ReasoningEffort | None:
    """收窄 reasoning effort，输入为字符串或 None，输出 AgentSpec 支持值。"""
    return coerce_reasoning_effort(value)


def _slug(value: str, *, max_len: int = 48) -> str:
    """生成 agent name 片段，输入为原文本，输出安全 slug。"""
    slug = _SLUG_RE.sub("-", value.strip()).strip("-_").lower()
    if not slug:
        slug = "task"
    return slug[:max_len]


__all__ = [
    "SpawnAgentRequest",
    "build_child_agent_spec",
    "build_spawn_request_from_tool_args",
    "build_spawn_request_from_workflow_task",
    "parent_agent_id_from_snapshot",
]
