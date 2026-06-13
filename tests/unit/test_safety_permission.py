"""unit：safety.policies.permission v0.1.4 占位形态 + from_config 迁移产物。

v0.1.4 起 ``PermissionPolicy.check`` / ``.evaluate`` 方法体已被
``raise NotImplementedError`` 替换；本文件仅验证 ``from_config`` 不抛异常 +
迁移产物形态正确。

完整 v0.1.3 → v0.1.4 端到端等价测试见 ``test_legacy_policy_migration.py``
（通过 SafetyDecisionEngine 走 e2e）。
"""

from __future__ import annotations

import pytest

from infrastructure.config.models import ApprovalConfig, Config, ModelConfig
from safety.approval.types import ApprovalRequiredCommand, SensitivePathRule
from safety.policies.permission import PermissionPolicy, PermissionRule


def _cfg(mode: str) -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="m",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        approval=ApprovalConfig(mode=mode),  # type: ignore[arg-type]
    )


@pytest.mark.unit
async def test_check_raises_not_implemented_in_v014() -> None:
    """v0.1.4 起 .check() 不再返回结果，而是 raise NotImplementedError。"""
    policy = PermissionPolicy()
    with pytest.raises(NotImplementedError, match="moved to SafetyDecisionEngine"):
        await policy.check("any", {})


@pytest.mark.unit
async def test_evaluate_raises_not_implemented_in_v014() -> None:
    """v0.1.3 别名 evaluate 同样 raise。"""
    policy = PermissionPolicy()
    with pytest.raises(NotImplementedError):
        await policy.evaluate("any", {})


@pytest.mark.unit
def test_from_config_maps_approval_mode_to_default_outcome() -> None:
    """v0.1.3 兼容字段：default_outcome 仍按 approval.mode 推导。"""
    p_ask = PermissionPolicy.from_config(_cfg("interactive"))
    assert p_ask.default_outcome == "ask"

    p_allow = PermissionPolicy.from_config(_cfg("auto_allow"))
    assert p_allow.default_outcome == "allow"

    p_deny = PermissionPolicy.from_config(_cfg("auto_deny"))
    assert p_deny.default_outcome == "deny"


@pytest.mark.unit
def test_path_deny_rule_translates_to_block_sensitive_path() -> None:
    rule = PermissionRule(
        tool_name="write_file",
        outcome="deny",
        arg_key="path",
        arg_contains=".ssh",
    )
    policy = PermissionPolicy([rule])
    sens = policy.migrated_sensitive_paths
    assert len(sens) == 1
    assert isinstance(sens[0], SensitivePathRule)
    assert sens[0].effect == "block"
    assert sens[0].matcher == ".ssh"


@pytest.mark.unit
def test_path_ask_rule_translates_to_elevated_sensitive_path() -> None:
    rule = PermissionRule(
        tool_name="write_file",
        outcome="ask",
        arg_key="path",
        arg_contains=".env",
    )
    policy = PermissionPolicy([rule])
    sens = policy.migrated_sensitive_paths
    assert len(sens) == 1
    assert sens[0].effect == "elevated"


@pytest.mark.unit
def test_tool_ask_rule_translates_to_approval_required_command() -> None:
    rule = PermissionRule(tool_name="run_shell", outcome="ask")
    policy = PermissionPolicy([rule])
    cmds = policy.migrated_approval_required_commands
    assert len(cmds) == 1
    assert isinstance(cmds[0], ApprovalRequiredCommand)
    assert cmds[0].severity == "standard"


@pytest.mark.unit
def test_path_allow_rule_translates_to_grant_candidate() -> None:
    rule = PermissionRule(
        tool_name="write_file",
        outcome="allow",
        arg_key="path",
        arg_contains="tests/",
    )
    policy = PermissionPolicy([rule])
    grants = policy.migrated_allow_path_grants
    assert grants == (("file_write", "tests/"),)
