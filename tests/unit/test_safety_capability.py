"""unit：safety.policies.capability v0.1.4 占位形态 + from_config 迁移产物。

v0.1.4 起 ``CapabilityPolicy.check`` 方法体已被 ``raise NotImplementedError``
替换；本文件仅验证 ``from_config`` 不抛异常 + 迁移产物形态正确。

完整 v0.1.3 → v0.1.4 端到端等价测试见 ``test_legacy_policy_migration.py``
（通过 SafetyDecisionEngine 走 e2e）。
"""

from __future__ import annotations

import pytest

from infrastructure.config.models import (
    Config,
    EvolutionConfig,
    EvolutionMemoryConfig,
    FileToolConfig,
    ModelConfig,
    ShellToolConfig,
    ToolConfig,
)
from safety.approval.types import HardDenyCommand
from safety.policies.capability import CapabilityPolicy, CapabilitySet


def _local_config(
    *,
    file_enabled: bool = True,
    shell_enabled: bool = True,
    memory_enabled: bool = True,
) -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="m",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        tool=ToolConfig(
            file=FileToolConfig(enabled=file_enabled),
            shell=ShellToolConfig(enabled=shell_enabled),
        ),
        evolution=EvolutionConfig(
            memory=EvolutionMemoryConfig(enabled=memory_enabled),
        ),
    )


@pytest.mark.unit
async def test_check_raises_not_implemented_in_v014() -> None:
    """v0.1.4 起 .check() 不再返回结果，而是 raise NotImplementedError。"""
    policy = CapabilityPolicy(CapabilitySet())
    with pytest.raises(NotImplementedError, match="moved to SafetyDecisionEngine"):
        await policy.check("read_file", {})


@pytest.mark.unit
def test_from_config_returns_instance_with_expected_allow_set() -> None:
    """from_config 在不抛异常的同时，capability_set 包含预期 allow capability。"""
    policy = CapabilityPolicy.from_config(_local_config())
    caps = policy.capability_set
    assert "file_read" in caps.allow
    assert "file_write" in caps.allow
    assert "shell" in caps.allow
    assert "memory" in caps.allow


@pytest.mark.unit
def test_from_config_excludes_shell_when_disabled() -> None:
    policy = CapabilityPolicy.from_config(_local_config(shell_enabled=False))
    assert "shell" not in policy.capability_set.allow


@pytest.mark.unit
def test_from_config_excludes_memory_when_disabled() -> None:
    policy = CapabilityPolicy.from_config(_local_config(memory_enabled=False))
    assert "memory" not in policy.capability_set.allow


@pytest.mark.unit
def test_capability_deny_translates_to_hard_deny_commands() -> None:
    """v0.1.4 迁移：capability deny 派生为 HardDenyCommand 元组。"""
    policy = CapabilityPolicy(CapabilitySet(deny=frozenset({"shell", "file_write"})))
    rules = policy.migrated_hard_deny_commands
    assert len(rules) == 2
    rule_names = {r.name for r in rules}
    assert "legacy-capability-deny-shell" in rule_names
    assert "legacy-capability-deny-file_write" in rule_names
    for r in rules:
        assert isinstance(r, HardDenyCommand)
        assert r.match_mode == "profile"
