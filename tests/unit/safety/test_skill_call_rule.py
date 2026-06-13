"""unit：v0.1.6 skill-system 模块 C 数据基座（``SkillCallRule`` + ``SafetySkillCallConfig``）。

覆盖：

1. ``safety.approval.types.SkillCallRule`` frozen dataclass 性
2. ``safety.approval.default_rules.DEFAULT_SKILL_CALL_RULES`` 默认空 tuple（deny-by-default 锚点）
3. ``infrastructure.config.models.SafetySkillCallConfig`` pydantic schema：
   - 有效配置加载
   - ``extra="forbid"`` 拒绝未知字段
   - ``name`` / ``matcher`` 空字符串拒绝
   - 非法 ``match_mode`` / ``effect`` / ``boundary_scope`` 由 pydantic Literal 校验拒绝
4. ``Config.safety.skill_call_rules`` yaml 加载：
   - 单条 allow rule
   - 多条 mixed effect
   - 缺失字段走 default_factory → 空 list
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from infrastructure.config.models import (
    Config,
    ModelConfig,
    SafetyConfig,
    SafetySkillCallConfig,
)
from safety.approval.default_rules import DEFAULT_SKILL_CALL_RULES
from safety.approval.types import BoundaryScope, SkillCallRule

# ---------------------------------------------------------------------------
# §1 SkillCallRule frozen dataclass
# ---------------------------------------------------------------------------


def test_skill_call_rule_is_frozen() -> None:
    rule = SkillCallRule(
        name="trusted-skill",
        matcher="my-skill",
        match_mode="exact",
        effect="allow",
        boundary_scope=BoundaryScope.ANY,
        reason="trusted",
    )
    with pytest.raises(FrozenInstanceError):
        rule.name = "mutated"  # type: ignore[misc]


def test_skill_call_rule_field_assignment_round_trip() -> None:
    """字段赋值（构造期）round-trip：确保 dataclass 字段定义完整、命名一致。"""
    rule = SkillCallRule(
        name="r",
        matcher="prefix:",
        match_mode="prefix",
        effect="elevated",
        boundary_scope=BoundaryScope.HOST,
        reason="probe",
    )
    assert rule.name == "r"
    assert rule.matcher == "prefix:"
    assert rule.match_mode == "prefix"
    assert rule.effect == "elevated"
    assert rule.boundary_scope is BoundaryScope.HOST
    assert rule.reason == "probe"


# ---------------------------------------------------------------------------
# §2 DEFAULT_SKILL_CALL_RULES = ()（deny-by-default 锚点）
# ---------------------------------------------------------------------------


def test_default_skill_call_rules_is_empty_tuple() -> None:
    """默认空 → 等价 deny-by-default（unhit 走 standard ask 分流）。

    若未来要内置默认规则，需配套更新 ConsentResolver / 设计文档；本测试是
    deny-by-default 不被无声破坏的锚点。
    """
    assert DEFAULT_SKILL_CALL_RULES == ()
    assert isinstance(DEFAULT_SKILL_CALL_RULES, tuple)


# ---------------------------------------------------------------------------
# §3 SafetySkillCallConfig pydantic schema
# ---------------------------------------------------------------------------


def test_safety_skill_call_config_valid_exact_allow() -> None:
    cfg = SafetySkillCallConfig(
        name="trust-doc-skill",
        matcher="doc-builder",
        match_mode="exact",
        effect="allow",
        boundary_scope="any",
        reason="internal documentation skill",
    )
    assert cfg.name == "trust-doc-skill"
    assert cfg.matcher == "doc-builder"
    assert cfg.match_mode == "exact"
    assert cfg.effect == "allow"
    assert cfg.boundary_scope == "any"
    assert cfg.reason == "internal documentation skill"


def test_safety_skill_call_config_default_boundary_scope_and_reason() -> None:
    """``boundary_scope`` 默认 ``"any"``、``reason`` 默认空字符串——yaml 简写场景。"""
    cfg = SafetySkillCallConfig(
        name="r",
        matcher="x",
        match_mode="exact",
        effect="block",
    )
    assert cfg.boundary_scope == "any"
    assert cfg.reason == ""


def test_safety_skill_call_config_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        SafetySkillCallConfig(  # type: ignore[call-arg]
            name="r",
            matcher="x",
            match_mode="exact",
            effect="allow",
            unknown_field="boom",
        )


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_safety_skill_call_config_rejects_empty_name(blank: str) -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        SafetySkillCallConfig(
            name=blank,
            matcher="x",
            match_mode="exact",
            effect="allow",
        )


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_safety_skill_call_config_rejects_empty_matcher(blank: str) -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        SafetySkillCallConfig(
            name="r",
            matcher=blank,
            match_mode="exact",
            effect="allow",
        )


def test_safety_skill_call_config_rejects_invalid_match_mode() -> None:
    with pytest.raises(ValidationError):
        SafetySkillCallConfig(
            name="r",
            matcher="x",
            match_mode="regex",  # type: ignore[arg-type]  # 非法
            effect="allow",
        )


def test_safety_skill_call_config_rejects_invalid_effect() -> None:
    with pytest.raises(ValidationError):
        SafetySkillCallConfig(
            name="r",
            matcher="x",
            match_mode="exact",
            effect="standard",  # type: ignore[arg-type]  # 非法（仅 block/elevated/allow）
        )


def test_safety_skill_call_config_rejects_invalid_boundary_scope() -> None:
    with pytest.raises(ValidationError):
        SafetySkillCallConfig(
            name="r",
            matcher="x",
            match_mode="exact",
            effect="allow",
            boundary_scope="cluster",  # type: ignore[arg-type]  # 非法
        )


# ---------------------------------------------------------------------------
# §4 Config.safety.skill_call_rules yaml 加载
# ---------------------------------------------------------------------------


def _local_model_yaml() -> dict[str, object]:
    """构造一份最小 ModelConfig yaml dict（本地端点跳过 api_key 校验）。"""
    return {
        "provider": "openai_compatible",
        "name": "m",
        "base_url": "http://127.0.0.1:1234",
        "api_key": "",
    }


def test_config_safety_skill_call_rules_default_empty() -> None:
    """缺省 yaml 时 ``Config.safety.skill_call_rules`` 默认空 list。"""
    cfg = Config.model_validate({"model": _local_model_yaml()})
    assert cfg.safety.skill_call_rules == []


def test_config_safety_skill_call_rules_single_allow() -> None:
    cfg = Config.model_validate(
        {
            "model": _local_model_yaml(),
            "safety": {
                "skill_call_rules": [
                    {
                        "name": "trust-internal",
                        "matcher": "internal:",
                        "match_mode": "prefix",
                        "effect": "allow",
                        "boundary_scope": "host",
                        "reason": "internal skills are trusted",
                    },
                ],
            },
        }
    )
    assert len(cfg.safety.skill_call_rules) == 1
    rule = cfg.safety.skill_call_rules[0]
    assert isinstance(rule, SafetySkillCallConfig)
    assert rule.name == "trust-internal"
    assert rule.matcher == "internal:"
    assert rule.match_mode == "prefix"
    assert rule.effect == "allow"
    assert rule.boundary_scope == "host"


def test_config_safety_skill_call_rules_mixed_effects() -> None:
    cfg = Config.model_validate(
        {
            "model": _local_model_yaml(),
            "safety": {
                "skill_call_rules": [
                    {
                        "name": "block-net",
                        "matcher": "net-tool",
                        "match_mode": "exact",
                        "effect": "block",
                        "boundary_scope": "any",
                        "reason": "network tool blocked",
                    },
                    {
                        "name": "elevated-deploy",
                        "matcher": "deploy-",
                        "match_mode": "prefix",
                        "effect": "elevated",
                        "boundary_scope": "any",
                        "reason": "deploy skills require elevated approval",
                    },
                    {
                        "name": "allow-doc",
                        "matcher": "doc-builder",
                        "match_mode": "exact",
                        "effect": "allow",
                        "boundary_scope": "any",
                        "reason": "doc skill",
                    },
                ],
            },
        }
    )
    assert [r.effect for r in cfg.safety.skill_call_rules] == [
        "block",
        "elevated",
        "allow",
    ]
    assert [r.match_mode for r in cfg.safety.skill_call_rules] == [
        "exact",
        "prefix",
        "exact",
    ]


def test_safety_config_skill_call_rules_field_default_factory() -> None:
    """直接构造 ``SafetyConfig`` 时也应 default 为空 list（不共享单例）。"""
    a = SafetyConfig()
    b = SafetyConfig()
    assert a.skill_call_rules == []
    assert b.skill_call_rules == []
    a.skill_call_rules.append(  # type: ignore[arg-type]
        SafetySkillCallConfig(
            name="r",
            matcher="x",
            match_mode="exact",
            effect="allow",
        )
    )
    # default_factory 不能让两个实例共享同一 list
    assert b.skill_call_rules == []


# ---------------------------------------------------------------------------
# §5 现有 Config 不受影响（向后兼容锚点：未填 skill_call_rules 的现有 yaml）
# ---------------------------------------------------------------------------


def test_existing_yaml_without_skill_call_rules_still_loads() -> None:
    """v0.1.5 之前 yaml 没有 skill_call_rules 字段，加载后默认空 list。"""
    cfg = Config.model_validate(
        {
            "model": _local_model_yaml(),
            "safety": {
                "hard_deny_commands": [],
                "approval_required_commands": [],
                "sensitive_paths": [],
                "trusted_workdirs": [],
                "allow_writes": [],
                "allow_tools_silent": [],
            },
        }
    )
    assert cfg.safety.skill_call_rules == []


# 用 ModelConfig 直接构造 cfg 不必走 yaml；参考现有测试：
def _local_model_config() -> ModelConfig:
    return ModelConfig(
        provider="openai_compatible",
        name="m",
        base_url="http://127.0.0.1:1234",
        api_key="",
    )


def test_config_default_safety_includes_skill_call_rules_field() -> None:
    """``Config(model=...)`` 默认装出来的 ``safety`` 含 ``skill_call_rules`` 字段。"""
    cfg = Config(model=_local_model_config())
    # 字段存在且默认空
    assert hasattr(cfg.safety, "skill_call_rules")
    assert cfg.safety.skill_call_rules == []
