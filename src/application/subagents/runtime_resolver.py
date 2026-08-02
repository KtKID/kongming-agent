"""子 agent 运行参数解析器。

本脚本负责把 agent.toml role 配置、父 agent 当前 run 快照和主配置合并成子 agent 实际运行参数。
作用是让 LLM 只提交任务语义，模型、推理等级、token、温度和超时等参数全部由代码侧解析。
关键执行流程：读取 task 的 agent_role_id 与调度 metadata，查 role 配置，按 role -> 父 agent -> 主配置顺序解析字段，再生成审计 payload。
关键函数：SubAgentRuntimeResolver.resolve 生成 ResolvedSubAgentRuntime，resolved_runtime_payload 序列化审计字段。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

from application.agent_roles import AgentRoleManager, AgentRolePreset
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.model_provider_catalog import ResolvedModelConfig
from infrastructure.config.models import Config, ReasoningEffortInput


@dataclass(frozen=True)
class ResolvedSubAgentRuntime:
    """子 agent 实际运行参数，输入来自配置和父 agent 快照，输出给 Runner。"""

    model: str
    preset_id: str
    reasoning_effort: str | None
    max_turns: int
    max_tokens: int
    temperature: float
    timeout_seconds: float
    field_sources: dict[str, str]
    parent_agent: dict[str, object]
    role_id: str | None = None
    role_nickname: str | None = None
    role_description: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    model_config: ResolvedModelConfig | None = field(default=None, repr=False)

    def to_payload(self) -> dict[str, object]:
        """序列化审计 payload，输入为解析结果，输出 JSON 友好字典。"""
        payload: dict[str, object] = {
            "model": self.model,
            "preset_id": self.preset_id,
            "reasoning_effort": self.reasoning_effort,
            "max_turns": self.max_turns,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "field_sources": dict(self.field_sources),
            "parent_agent": dict(self.parent_agent),
            "role_id": self.role_id,
            "role_nickname": self.role_nickname,
        }
        if self.role_description:
            payload["role_description"] = self.role_description
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


class SubAgentRuntimeResolver:
    """按配置和父 agent 快照解析子 agent 运行参数。"""

    def __init__(
        self,
        config: Config,
        role_manager: AgentRoleManager,
        *,
        model_catalog_manager: ModelCatalogManager | None = None,
    ) -> None:
        """初始化解析器，输入为全局配置和角色管理器，输出可复用 resolver。"""
        self._config = config
        self._role_manager = role_manager
        self._model_catalog_manager = model_catalog_manager or ModelCatalogManager()
        self._default_model_config = self._model_catalog_manager.resolve_runtime(config.model)

    @property
    def default_model_config(self) -> ResolvedModelConfig:
        """返回当前 resolver 绑定的默认 immutable 模型快照。"""
        return self._default_model_config

    def resolve(
        self,
        *,
        agent_role_id: str | None,
        task_metadata: Mapping[str, object],
        parent_agent: Mapping[str, object] | None,
    ) -> ResolvedSubAgentRuntime:
        """解析子 agent runtime，输入为 role id、调度 metadata、父 agent 快照，输出实际参数。"""
        parent_payload = _normalize_parent_agent(parent_agent)
        role = self._resolve_role(agent_role_id)
        field_sources: dict[str, str] = {}

        preset_id, preset_source = _first_string(
            (role.model if role is not None else None, _source_for_role(role, "model")),
            (_parent_preset_id(parent_payload), "parent_agent.preset_id"),
            (self._config.model.preset_id, "config.model.preset_id"),
        )
        field_sources["preset_id"] = preset_source

        reasoning_effort, reasoning_source = _first_optional_string(
            (
                role.reasoning_effort if role is not None else None,
                _source_for_role(role, "reasoning_effort"),
            ),
            (_parent_reasoning(parent_payload), "parent_agent.reasoning_effort"),
            (self._config.model.reasoning_effort, "config.model.reasoning_effort"),
        )
        field_sources["reasoning_effort"] = reasoning_source

        model_config = self._model_catalog_manager.resolve_runtime(
            self._config.model,
            preset_id=preset_id,
            reasoning_effort=cast(ReasoningEffortInput | None, reasoning_effort),
        )
        field_sources["model"] = f"catalog:{model_config.preset_id}"

        max_turns, max_turns_source = _first_positive_int(
            (task_metadata.get("max_turns"), "schedule.max_turns"),
            (role.max_turns if role is not None else None, _source_for_role(role, "max_turns")),
            (_parent_max_turns(parent_payload), "parent_agent.max_turns"),
            (self._config.runner.max_turns, "config.runner.max_turns"),
        )
        field_sources["max_turns"] = max_turns_source

        max_tokens, max_tokens_source = _first_positive_int(
            (task_metadata.get("max_tokens"), "schedule.max_tokens"),
            (_parent_int(parent_payload, "max_tokens"), "parent_agent.max_tokens"),
            (model_config.max_tokens, f"catalog:{model_config.preset_id}.max_tokens"),
        )
        field_sources["max_tokens"] = max_tokens_source

        temperature, temperature_source = _first_temperature(
            (task_metadata.get("temperature"), "schedule.temperature"),
            (_parent_temperature(parent_payload), "parent_agent.temperature"),
            (model_config.temperature, f"catalog:{model_config.preset_id}.temperature"),
        )
        field_sources["temperature"] = temperature_source

        timeout_seconds, timeout_source = _first_float(
            (task_metadata.get("timeout_seconds"), "schedule.timeout_seconds"),
            (_parent_float(parent_payload, "timeout_seconds"), "parent_agent.timeout_seconds"),
            (model_config.timeout, f"catalog:{model_config.preset_id}.timeout"),
        )
        field_sources["timeout_seconds"] = timeout_source

        return ResolvedSubAgentRuntime(
            model=model_config.name,
            preset_id=model_config.preset_id,
            reasoning_effort=model_config.default_reasoning_effort,
            max_turns=max_turns,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            field_sources=field_sources,
            parent_agent=parent_payload,
            role_id=role.role_id if role is not None else agent_role_id,
            role_nickname=role.nickname if role is not None else None,
            role_description=role.role_desc if role is not None else None,
            model_config=model_config,
        )

    def _resolve_role(self, role_id: str | None) -> AgentRolePreset | None:
        """查询 role，输入为 role id，输出角色或 None；显式 id 缺失时报错。"""
        if role_id is None or not role_id.strip():
            return None
        role = self._role_manager.get_role(role_id)
        if role is None:
            raise ValueError(f"unknown subagent role id: {role_id}")
        return role


def resolved_runtime_payload(runtime: ResolvedSubAgentRuntime | None) -> dict[str, object] | None:
    """序列化可选 runtime，输入为解析结果或 None，输出审计 payload 或 None。"""
    if runtime is None:
        return None
    return runtime.to_payload()


def _normalize_parent_agent(parent_agent: Mapping[str, object] | None) -> dict[str, object]:
    """归一化父 agent 快照，输入为 metadata 字段，输出可审计字典。"""
    if parent_agent is None:
        return {}
    return {str(key): value for key, value in parent_agent.items()}


def _source_for_role(role: AgentRolePreset | None, field_name: str) -> str:
    """生成 role 字段来源，输入为 role 和字段名，输出来源字符串。"""
    if role is None:
        return ""
    return f"agent.toml:{role.role_id}.{field_name}"


def _parent_preset_id(parent_agent: Mapping[str, object]) -> str | None:
    """读取父 agent preset，输入为快照，输出 preset ID 或 None。"""
    direct = parent_agent.get("preset_id")
    if isinstance(direct, str) and direct.strip():
        return direct
    return None


def _parent_reasoning(parent_agent: Mapping[str, object]) -> str | None:
    """读取父 agent reasoning，输入为快照，输出推理等级或 None。"""
    direct = parent_agent.get("reasoning_effort")
    if isinstance(direct, str) and direct.strip():
        return direct
    agent_spec = parent_agent.get("agent_spec")
    if isinstance(agent_spec, Mapping):
        spec_reasoning = agent_spec.get("reasoning_effort")
        if isinstance(spec_reasoning, str) and spec_reasoning.strip():
            return spec_reasoning
    return None


def _parent_max_turns(parent_agent: Mapping[str, object]) -> int | None:
    """读取父 agent turn 上限，输入为快照，输出正整数或 None。"""
    for key in ("effective_max_turns", "max_turns"):
        value = _parent_int(parent_agent, key)
        if value is not None:
            return value
    agent_spec = parent_agent.get("agent_spec")
    if isinstance(agent_spec, Mapping):
        value = agent_spec.get("max_turns")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _parent_int(parent_agent: Mapping[str, object], key: str) -> int | None:
    """读取父 agent 整数字段，输入为快照和 key，输出正整数或 None。"""
    value = parent_agent.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _parent_float(parent_agent: Mapping[str, object], key: str) -> float | None:
    """读取父 agent 浮点字段，输入为快照和 key，输出正数或 None。"""
    value = parent_agent.get(key)
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def _parent_temperature(parent_agent: Mapping[str, object]) -> float | None:
    """读取父 agent temperature，输入为快照，输出合法温度或 None。"""
    value = parent_agent.get("temperature")
    if isinstance(value, int | float) and not isinstance(value, bool) and 0 <= float(value) <= 2:
        return float(value)
    return None


def _first_string(*candidates: tuple[object, str]) -> tuple[str, str]:
    """选择首个非空字符串，输入为候选值和来源，输出值和来源。"""
    for value, source in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip(), source
    raise ValueError("subagent runtime model cannot be resolved")


def _first_optional_string(*candidates: tuple[object, str]) -> tuple[str | None, str]:
    """选择首个非空字符串，输入为候选值和来源，输出可空值和来源。"""
    for value, source in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip(), source
    return None, "provider_default"


def _first_positive_int(*candidates: tuple[object, str]) -> tuple[int, str]:
    """选择首个正整数，输入为候选值和来源，输出值和来源。"""
    for value, source in candidates:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value, source
    raise ValueError("subagent runtime positive integer field cannot be resolved")


def _first_float(*candidates: tuple[object, str]) -> tuple[float, str]:
    """选择首个正数，输入为候选值和来源，输出浮点值和来源。"""
    for value, source in candidates:
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return float(value), source
    raise ValueError("subagent runtime float field cannot be resolved")


def _first_temperature(*candidates: tuple[object, str]) -> tuple[float, str]:
    """选择首个合法温度，输入为候选值和来源，输出温度和来源。"""
    for value, source in candidates:
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and 0 <= float(value) <= 2
        ):
            return float(value), source
    raise ValueError("subagent runtime temperature field cannot be resolved")


__all__ = [
    "ResolvedSubAgentRuntime",
    "SubAgentRuntimeResolver",
    "resolved_runtime_payload",
]
