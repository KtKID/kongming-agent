"""子 agent runtime 通过 preset snapshot 解析的单元测试。"""

from __future__ import annotations

from pathlib import Path

from application.agent_roles import AgentRoleManager
from application.subagents.runtime_resolver import SubAgentRuntimeResolver
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config, ModelSelectionConfig, RunnerConfig

_CATALOG = """\
version: 2
providers:
  - provider_id: test
    default_preset_id: config-preset
    display_name: Test
    region_label: Local
    description: test provider
    logo_text: T
    protocol: openai
    default_base_url: http://127.0.0.1:8000/v1
    request_defaults:
      timeout_seconds: 30
      max_tokens: 4096
      temperature: 0.4
    models:
      - preset_id: config-preset
        model: config-model
        reasoning: &reasoning
          adapter: glm_thinking_toggle
          supported_efforts: [low, medium, high]
          default_effort: low
          supports_disabled: true
      - preset_id: parent-preset
        model: parent-model
        reasoning: *reasoning
      - preset_id: role-preset
        model: role-model
        reasoning: *reasoning
"""


def _config() -> Config:
    """构造只含运行选择的配置。"""
    return Config(
        model=ModelSelectionConfig(preset_id="config-preset", reasoning_effort="low"),
        runner=RunnerConfig(max_turns=6),
    )


def _role_manager(tmp_path: Path) -> AgentRoleManager:
    """构造把 role.model 解释为 preset ID 的角色配置。"""
    agent_config = tmp_path / "agent.toml"
    agent_config.write_text(
        """
[[agents]]
id = 1
nickname = "Architect"
model = "role-preset"
role_desc = "审查架构边界"
reasoning_effort = "high"
max_turns = 4
""".strip(),
        encoding="utf-8",
    )
    return AgentRoleManager(role_dir=tmp_path / "roles", config_path=agent_config)


def _resolver(tmp_path: Path) -> SubAgentRuntimeResolver:
    """构造注入测试 catalog 的 resolver。"""
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(_CATALOG, encoding="utf-8")
    manager = ModelCatalogManager(builtin_path=catalog_path, user_path=tmp_path / "missing.yaml")
    return SubAgentRuntimeResolver(
        _config(),
        _role_manager(tmp_path),
        model_catalog_manager=manager,
    )


def test_resolver_prefers_role_preset_and_effort(tmp_path: Path) -> None:
    runtime = _resolver(tmp_path).resolve(
        agent_role_id="1",
        task_metadata={},
        parent_agent={
            "preset_id": "parent-preset",
            "reasoning_effort": "medium",
            "max_turns": 9,
            "max_tokens": 8192,
            "temperature": 0.2,
            "timeout_seconds": 90.0,
        },
    )

    assert runtime.preset_id == "role-preset"
    assert runtime.model == "role-model"
    assert runtime.reasoning_effort == "high"
    assert runtime.max_turns == 4
    assert runtime.max_tokens == 8192
    assert runtime.field_sources["preset_id"] == "agent.toml:1.model"
    assert runtime.model_config is not None
    assert runtime.model_config.preset_id == "role-preset"


def test_resolver_uses_parent_snapshot_without_role(tmp_path: Path) -> None:
    runtime = _resolver(tmp_path).resolve(
        agent_role_id=None,
        task_metadata={},
        parent_agent={
            "preset_id": "parent-preset",
            "reasoning_effort": "medium",
            "max_turns": 9,
            "max_tokens": 8192,
        },
    )

    assert runtime.preset_id == "parent-preset"
    assert runtime.model == "parent-model"
    assert runtime.reasoning_effort == "medium"
    assert runtime.max_turns == 9
    assert runtime.max_tokens == 8192
    assert runtime.temperature == 0.4
    assert runtime.timeout_seconds == 30.0
    assert runtime.field_sources["preset_id"] == "parent_agent.preset_id"


def test_schedule_overrides_numeric_runtime_fields(tmp_path: Path) -> None:
    runtime = _resolver(tmp_path).resolve(
        agent_role_id="1",
        task_metadata={"max_turns": 2, "temperature": 0.0},
        parent_agent={"preset_id": "parent-preset", "temperature": 0.7},
    )

    assert runtime.max_turns == 2
    assert runtime.temperature == 0.0
    assert runtime.field_sources["max_turns"] == "schedule.max_turns"
    assert runtime.field_sources["temperature"] == "schedule.temperature"
