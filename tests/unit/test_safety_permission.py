"""unit：safety.permission_policy 规则匹配 + default_outcome。"""

from __future__ import annotations

import pytest

from config_loader.models import ApprovalConfig, Config, ModelConfig
from safety.permission_policy import PermissionPolicy, PermissionRule


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
async def test_no_rules_returns_default_ask() -> None:
    policy = PermissionPolicy()
    result = await policy.check("any", {})
    assert result.outcome == "ask"
    assert result.matched_rule is None


@pytest.mark.unit
async def test_default_outcome_can_be_allow() -> None:
    policy = PermissionPolicy(default_outcome="allow")
    result = await policy.check("any", {})
    assert result.outcome == "allow"


@pytest.mark.unit
async def test_default_outcome_can_be_deny() -> None:
    policy = PermissionPolicy(default_outcome="deny")
    result = await policy.check("any", {})
    assert result.outcome == "deny"


@pytest.mark.unit
async def test_rule_matches_by_tool_name() -> None:
    rule = PermissionRule(tool_name="read_file", outcome="allow")
    policy = PermissionPolicy([rule])

    result = await policy.check("read_file", {})
    assert result.outcome == "allow"
    assert result.matched_rule is rule


@pytest.mark.unit
async def test_wildcard_tool_name_matches_everything() -> None:
    rule = PermissionRule(tool_name="*", outcome="deny", reason="blanket deny")
    policy = PermissionPolicy([rule])

    result = await policy.check("any_tool", {})
    assert result.outcome == "deny"
    assert result.matched_rule is rule


@pytest.mark.unit
async def test_arg_contains_filters_rule_match() -> None:
    rule = PermissionRule(
        tool_name="run_shell",
        outcome="deny",
        arg_key="command",
        arg_contains="rm -rf",
    )
    policy = PermissionPolicy([rule], default_outcome="allow")

    # 命中危险 arg → 规则生效
    r1 = await policy.check("run_shell", {"command": "rm -rf /tmp/x"})
    assert r1.outcome == "deny"

    # 没命中 arg → 走 default
    r2 = await policy.check("run_shell", {"command": "echo hi"})
    assert r2.outcome == "allow"
    assert r2.matched_rule is None


@pytest.mark.unit
async def test_first_matching_rule_wins() -> None:
    r1 = PermissionRule(tool_name="read_file", outcome="allow", reason="first")
    r2 = PermissionRule(tool_name="read_file", outcome="deny", reason="second")
    policy = PermissionPolicy([r1, r2])

    result = await policy.check("read_file", {})
    assert result.outcome == "allow"
    assert result.matched_rule is r1


@pytest.mark.unit
async def test_from_config_maps_approval_mode_to_default_outcome() -> None:
    p_ask = PermissionPolicy.from_config(_cfg("interactive"))
    assert (await p_ask.check("x", {})).outcome == "ask"

    p_allow = PermissionPolicy.from_config(_cfg("auto_allow"))
    assert (await p_allow.check("x", {})).outcome == "allow"

    p_deny = PermissionPolicy.from_config(_cfg("auto_deny"))
    assert (await p_deny.check("x", {})).outcome == "deny"


@pytest.mark.unit
async def test_arg_key_present_but_non_string_value_does_not_match() -> None:
    rule = PermissionRule(
        tool_name="*",
        outcome="deny",
        arg_key="threshold",
        arg_contains="5",
    )
    policy = PermissionPolicy([rule], default_outcome="allow")
    # 数字类型 arg 不匹配 → 走 default allow
    result = await policy.check("t", {"threshold": 5})
    assert result.outcome == "allow"
