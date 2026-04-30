"""unit：v0.1.3 → v0.1.4 老 policy 迁移测试矩阵。

覆盖任务 #37/#38 要求：

- v0.1.3 ``CapabilityPolicy.from_config`` 把 capability deny → HardDenyCommand
- v0.1.3 ``PermissionPolicy.from_config`` / 显式 PermissionRule 列表的迁移：
  - ``outcome=deny + path`` → ``SensitivePathRule(effect="block")``
  - ``outcome=ask + path`` → ``SensitivePathRule(effect="elevated")``
  - ``outcome=ask + tool_name`` → ``ApprovalRequiredCommand(severity="standard")``
  - ``outcome=allow + path`` → grant 候选元组 (capability, path_prefix)
- 12 种典型 v0.1.3 配置形态都不抛异常
"""

from __future__ import annotations

import pytest

from config_loader.models import (
    ApprovalConfig,
    Config,
    EvolutionConfig,
    EvolutionMemoryConfig,
    FileToolConfig,
    ModelConfig,
    ShellToolConfig,
    ToolConfig,
)
from safety.capability_policy import CapabilityPolicy, CapabilitySet
from safety.permission_policy import PermissionPolicy, PermissionRule


def _config(
    *,
    file_enabled: bool = True,
    shell_enabled: bool = True,
    memory_enabled: bool = False,
    approval_mode: str = "interactive",
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
        approval=ApprovalConfig(mode=approval_mode),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# CapabilityPolicy 迁移
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_capability_allow_full_default() -> None:
    """case 1: capability allow={file_read, file_write} deny=空 → 不派生 hard_deny。"""
    policy = CapabilityPolicy.from_config(_config(file_enabled=True))
    assert policy.migrated_hard_deny_commands == ()
    assert "file_read" in policy.capability_set.allow
    assert "file_write" in policy.capability_set.allow


@pytest.mark.unit
def test_capability_explicit_deny_set_translates_to_rules() -> None:
    """case 2: 显式 capability deny={shell} → 派生 1 条 HardDenyCommand。"""
    policy = CapabilityPolicy(CapabilitySet(deny=frozenset({"shell"})))
    rules = policy.migrated_hard_deny_commands
    assert len(rules) == 1
    assert rules[0].name == "legacy-capability-deny-shell"
    assert rules[0].match_mode == "profile"


# ---------------------------------------------------------------------------
# PermissionPolicy 迁移：path 类规则
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_permission_rule_deny_path_translates_to_block() -> None:
    """case 3: rule(tool_name=write_file, arg_contains=.ssh, outcome=deny)
    → SensitivePathRule(effect=block)。"""
    rule = PermissionRule(
        tool_name="write_file",
        outcome="deny",
        arg_key="path",
        arg_contains="/home/user/.ssh",
    )
    policy = PermissionPolicy([rule])
    sens = policy.migrated_sensitive_paths
    assert len(sens) == 1
    assert sens[0].effect == "block"
    assert sens[0].matcher == "/home/user/.ssh"


@pytest.mark.unit
def test_permission_rule_allow_path_translates_to_grant() -> None:
    """case 4: rule(tool_name=*, arg_contains=tests/, outcome=allow)
    → grant 候选 (file_write, tests/)。"""
    rule = PermissionRule(
        tool_name="*",
        outcome="allow",
        arg_key="path",
        arg_contains="tests/",
    )
    policy = PermissionPolicy([rule])
    grants = policy.migrated_allow_path_grants
    assert grants == (("file_write", "tests/"),)


@pytest.mark.unit
def test_permission_rule_ask_path_translates_to_elevated() -> None:
    """case 5: rule(tool_name=run_shell, arg_contains=git push, outcome=ask)
    → 因 arg_key 为 path 类（不在 path 子集里），落到工具级 approval_required。

    这是任务 case 5 的语义校准：v0.1.3 用户在 arg_key=command 时是 tool 级
    ask，迁移到 v0.1.4 应翻译为 approval_required_commands(standard) 而非
    sensitive_paths（command 不是 path）。
    """
    rule = PermissionRule(
        tool_name="run_shell",
        outcome="ask",
        arg_key="command",  # command 不在 {path,file_path,target} 子集里
        arg_contains="git push",
    )
    policy = PermissionPolicy([rule])
    # arg_key="command" 不识别为 path → 走工具级 ask 分支
    cmds = policy.migrated_approval_required_commands
    assert len(cmds) == 1
    assert cmds[0].severity == "standard"


# ---------------------------------------------------------------------------
# 混合配置 / 默认 / 边角
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mixed_capability_and_permission_does_not_raise() -> None:
    """case 6: capability + permission 同时存在的混合配置不抛异常。"""
    cap_policy = CapabilityPolicy(CapabilitySet(deny=frozenset({"shell"})))
    perm_policy = PermissionPolicy(
        [
            PermissionRule(
                tool_name="write_file",
                outcome="deny",
                arg_key="path",
                arg_contains=".ssh",
            ),
        ]
    )
    assert len(cap_policy.migrated_hard_deny_commands) == 1
    assert len(perm_policy.migrated_sensitive_paths) == 1


@pytest.mark.unit
def test_empty_permission_policy_default_outcome() -> None:
    """case 7: 空 rules + default_outcome=ask（v0.1.3 默认）。"""
    policy = PermissionPolicy.from_config(_config(approval_mode="interactive"))
    assert policy.default_outcome == "ask"
    assert policy.migrated_sensitive_paths == ()


@pytest.mark.unit
def test_default_outcome_deny_vs_ask() -> None:
    """case 8: approval mode auto_deny / interactive 都不抛异常。"""
    p_deny = PermissionPolicy.from_config(_config(approval_mode="auto_deny"))
    assert p_deny.default_outcome == "deny"
    p_ask = PermissionPolicy.from_config(_config(approval_mode="interactive"))
    assert p_ask.default_outcome == "ask"


@pytest.mark.unit
def test_memory_capability_in_v013_config_translates() -> None:
    """case 9: 含 memory capability 的配置（v0.1.3 新增）—— 应进入 allow。"""
    policy = CapabilityPolicy.from_config(_config(memory_enabled=True))
    assert "memory" in policy.capability_set.allow


@pytest.mark.unit
def test_minimal_config_no_safety_section_works() -> None:
    """case 10: 没有 safety section 的最小配置 → 默认 SafetyConfig。"""
    cfg = _config()
    cap = CapabilityPolicy.from_config(cfg)
    perm = PermissionPolicy.from_config(cfg)
    assert cap is not None
    assert perm is not None
    # 没有规则就没有迁移产物
    assert perm.migrated_sensitive_paths == ()
    assert perm.migrated_approval_required_commands == ()


@pytest.mark.unit
def test_arg_contains_with_regex_chars_does_not_raise() -> None:
    """case 11: arg_contains 含 regex 字符（``.*``、``[abc]``）转 path_prefix 时
    不会爆 regex 错误（path_prefix 是字符串字面量，不解析 regex）。"""
    rule = PermissionRule(
        tool_name="write_file",
        outcome="deny",
        arg_key="path",
        arg_contains=".*",  # 看似 regex，但 path_prefix 当字面量处理
    )
    policy = PermissionPolicy([rule])
    sens = policy.migrated_sensitive_paths
    assert len(sens) == 1
    # matcher 直接保留原始字符串（不预处理 regex）
    assert sens[0].matcher == ".*"


@pytest.mark.unit
def test_unknown_arg_key_falls_back_to_tool_level_ask() -> None:
    """case 12: 未知 arg_key 不会破坏迁移；按工具级语义走（如 outcome=ask 时）。"""
    rule = PermissionRule(
        tool_name="custom_tool",
        outcome="ask",
        arg_key="unknown_field",
        arg_contains="value",
    )
    policy = PermissionPolicy([rule])
    # arg_key=unknown_field 不在 path 子集 → 工具级 ask
    cmds = policy.migrated_approval_required_commands
    assert len(cmds) == 1
    assert cmds[0].name.startswith("legacy-permission-ask-tool-")
