"""unit：v0.1.6 skill-system 模块 C —— ConsentResolver skill 分支分类逻辑。

覆盖目标：

- :func:`safety.guards.consent._skill_name_matches`：exact / prefix / 空 name
- :meth:`safety.guards.consent.ConsentResolver._classify_skill`：
  block > elevated > allow > standard fallback 优先级链
- ``boundary_scope=SANDBOX`` 规则被构造时过滤
- :func:`safety.guards.consent._normalize_user_skill_call_rules`（test of internal helper）
- :data:`safety.default_rules.DEFAULT_SKILL_CALL_RULES` 默认空 tuple

不覆盖 evaluate 主流程 silent_allow 分流（见 ``test_consent_skill_silent_allow.py``）。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.contracts import ApprovalDecision, ApprovalRequest
from infrastructure.config.models import ApprovalConfig, Config, ModelConfig, SafetySkillCallConfig
from safety.default_rules import DEFAULT_SKILL_CALL_RULES
from safety.guards.consent import (
    ConsentResolver,
    _normalize_user_skill_call_rules,  # test of internal helper
    _skill_name_matches,  # test of internal helper
)
from safety.types import BoundaryScope, SkillCallRule

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _cfg() -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="m",
            base_url="http://127.0.0.1:1234",
            api_key="",
        ),
        approval=ApprovalConfig(mode="interactive"),
    )


class _NeverApproval:
    """ApprovalProvider stub：本文件测试只用 _classify_skill，永不应被调到。"""

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:  # pragma: no cover
        raise AssertionError(
            "InteractiveApproval.decide must not be invoked from _classify_skill tests"
        )


def _make_resolver_with_rules(
    rules: tuple[SkillCallRule, ...],
) -> ConsentResolver:
    """直接传 skill_call_rules 给 __init__，绕开 from_config 的 default 合并。"""
    return ConsentResolver(
        approval_required_commands=(),
        sensitive_paths=(),
        skill_call_rules=rules,
        interactive_approval=_NeverApproval(),
    )


def _classify(resolver: ConsentResolver, *, name: Any) -> Any:
    """直接调内部 _classify_skill，返回 _ConsentHit。"""
    return resolver._classify_skill({"skill": name})


# ---------------------------------------------------------------------------
# §1 _skill_name_matches —— exact / prefix / empty
# ---------------------------------------------------------------------------


def test_skill_name_matches_exact() -> None:
    """``match_mode="exact"``：名字相等 True，否则 False。"""
    rule = SkillCallRule(
        name="r",
        matcher="my-skill",
        match_mode="exact",
        effect="allow",
        boundary_scope=BoundaryScope.ANY,
        reason="",
    )
    assert _skill_name_matches(rule, "my-skill") is True
    assert _skill_name_matches(rule, "my-skill-2") is False
    assert _skill_name_matches(rule, "your-skill") is False


def test_skill_name_matches_prefix() -> None:
    """``match_mode="prefix"``：名字以 matcher 起首 True，否则 False。"""
    rule = SkillCallRule(
        name="r",
        matcher="internal:",
        match_mode="prefix",
        effect="allow",
        boundary_scope=BoundaryScope.ANY,
        reason="",
    )
    assert _skill_name_matches(rule, "internal:foo") is True
    assert _skill_name_matches(rule, "internal:") is True  # 等长前缀
    assert _skill_name_matches(rule, "external:foo") is False
    assert _skill_name_matches(rule, "intern") is False  # matcher 比 name 长


def test_skill_name_matches_empty() -> None:
    """空名字一律不匹配（防御性）。"""
    exact_rule = SkillCallRule(
        name="r",
        matcher="x",
        match_mode="exact",
        effect="allow",
        boundary_scope=BoundaryScope.ANY,
        reason="",
    )
    prefix_rule = SkillCallRule(
        name="r",
        matcher="x",
        match_mode="prefix",
        effect="allow",
        boundary_scope=BoundaryScope.ANY,
        reason="",
    )
    assert _skill_name_matches(exact_rule, "") is False
    assert _skill_name_matches(prefix_rule, "") is False


# ---------------------------------------------------------------------------
# §2 _classify_skill 优先级链：block > elevated > allow > standard
# ---------------------------------------------------------------------------


def test_classify_skill_block_wins() -> None:
    """同一 skill 命中 block + elevated + allow，block 优先（severity=elevated 但
    reason 含 ``block:`` 前缀，matched_rule 取 block 规则名）。"""
    rules = (
        SkillCallRule(
            name="rule-block",
            matcher="dangerous",
            match_mode="exact",
            effect="block",
            boundary_scope=BoundaryScope.ANY,
            reason="this is dangerous",
        ),
        SkillCallRule(
            name="rule-elevated",
            matcher="dangerous",
            match_mode="exact",
            effect="elevated",
            boundary_scope=BoundaryScope.ANY,
            reason="elevated reason",
        ),
        SkillCallRule(
            name="rule-allow",
            matcher="dangerous",
            match_mode="exact",
            effect="allow",
            boundary_scope=BoundaryScope.ANY,
            reason="allow reason",
        ),
    )
    resolver = _make_resolver_with_rules(rules)
    hit = _classify(resolver, name="dangerous")
    assert hit.severity == "elevated"
    assert hit.matched_rule == "rule-block"
    assert hit.reason.startswith("block:")
    assert "this is dangerous" in hit.reason
    assert hit.target_value == "dangerous"


def test_classify_skill_elevated_when_no_block() -> None:
    """无 block 命中、elevated 命中 → severity=elevated，matched_rule 取 elevated 规则。"""
    rules = (
        SkillCallRule(
            name="rule-elevated",
            matcher="deploy-",
            match_mode="prefix",
            effect="elevated",
            boundary_scope=BoundaryScope.ANY,
            reason="deploy needs elevated",
        ),
        SkillCallRule(
            name="rule-allow",
            matcher="deploy-staging",
            match_mode="exact",
            effect="allow",
            boundary_scope=BoundaryScope.ANY,
            reason="staging is safe",
        ),
    )
    resolver = _make_resolver_with_rules(rules)
    hit = _classify(resolver, name="deploy-staging")
    # elevated 优先于 allow
    assert hit.severity == "elevated"
    assert hit.matched_rule == "rule-elevated"
    assert hit.reason == "deploy needs elevated"
    assert hit.target_value == "deploy-staging"


def test_classify_skill_allow_when_no_block_elevated() -> None:
    """仅 allow 命中 → severity=allow，reason / matched_rule 正确填充。"""
    rules = (
        SkillCallRule(
            name="rule-allow",
            matcher="doc-builder",
            match_mode="exact",
            effect="allow",
            boundary_scope=BoundaryScope.ANY,
            reason="trusted doc skill",
        ),
    )
    resolver = _make_resolver_with_rules(rules)
    hit = _classify(resolver, name="doc-builder")
    assert hit.severity == "allow"
    assert hit.matched_rule == "rule-allow"
    assert hit.reason == "trusted doc skill"
    assert hit.target_value == "doc-builder"


def test_classify_skill_default_standard() -> None:
    """无任何 rule 命中 → standard fallback，matched_rule="skill-call-default"。"""
    resolver = _make_resolver_with_rules(())
    hit = _classify(resolver, name="some-unknown-skill")
    assert hit.severity == "standard"
    assert hit.matched_rule == "skill-call-default"
    assert "未配置" in hit.reason or "未配" in hit.reason
    assert hit.target_value == "some-unknown-skill"


def test_classify_skill_empty_name_returns_standard() -> None:
    """skill 名为空 / 非 string → standard fallback，matched_rule="skill-empty-name"。"""
    resolver = _make_resolver_with_rules(
        (
            SkillCallRule(
                name="rule-allow",
                matcher="",  # 空 matcher 不会真用到 —— consent yaml 校验已禁，但运行时
                # 仍可通过直构 SkillCallRule 跳过校验，因此 _classify_skill 必须先过
                # 空名守卫
                match_mode="prefix",
                effect="allow",
                boundary_scope=BoundaryScope.ANY,
                reason="",
            ),
        )
    )
    hit_empty = resolver._classify_skill({"skill": ""})
    assert hit_empty.matched_rule == "skill-empty-name"
    assert hit_empty.severity == "standard"

    hit_blank = resolver._classify_skill({"skill": "   "})
    assert hit_blank.matched_rule == "skill-empty-name"

    hit_none = resolver._classify_skill({"skill": None})
    assert hit_none.matched_rule == "skill-empty-name"


# ---------------------------------------------------------------------------
# §3 boundary_scope 过滤：构造时 SANDBOX 规则被剔除
# ---------------------------------------------------------------------------


def test_classify_skill_boundary_scope_filter() -> None:
    """直构 SkillCallRule 时塞 boundary_scope=SANDBOX，应被构造期过滤掉。

    剔除后 ``_classify_skill`` 走 standard fallback；HOST / ANY 规则正常生效。
    """
    rules = (
        # SANDBOX 规则会被 ConsentResolver.__init__ 过滤
        SkillCallRule(
            name="sandbox-allow",
            matcher="sandboxed",
            match_mode="exact",
            effect="allow",
            boundary_scope=BoundaryScope.SANDBOX,
            reason="sandbox-only",
        ),
        # HOST 规则保留
        SkillCallRule(
            name="host-allow",
            matcher="host-skill",
            match_mode="exact",
            effect="allow",
            boundary_scope=BoundaryScope.HOST,
            reason="host trusted",
        ),
        # ANY 规则保留
        SkillCallRule(
            name="any-block",
            matcher="forbidden",
            match_mode="exact",
            effect="block",
            boundary_scope=BoundaryScope.ANY,
            reason="any-scope block",
        ),
    )
    resolver = _make_resolver_with_rules(rules)

    # SANDBOX rule 被过滤 → sandboxed 走 standard fallback
    hit_sandbox = _classify(resolver, name="sandboxed")
    assert hit_sandbox.matched_rule == "skill-call-default"
    assert hit_sandbox.severity == "standard"

    # HOST rule 仍生效
    hit_host = _classify(resolver, name="host-skill")
    assert hit_host.severity == "allow"
    assert hit_host.matched_rule == "host-allow"

    # ANY rule 仍生效
    hit_any = _classify(resolver, name="forbidden")
    assert hit_any.severity == "elevated"
    assert hit_any.matched_rule == "any-block"


# ---------------------------------------------------------------------------
# §4 _normalize_user_skill_call_rules —— yaml → SkillCallRule 转换
# ---------------------------------------------------------------------------


def test_normalize_user_skill_call_rules_round_trip() -> None:
    """yaml 配置 ``boundary_scope="any"`` 转 :class:`BoundaryScope.ANY` 枚举，字段全保留。"""
    user_cfgs = [
        SafetySkillCallConfig(
            name="trust-doc",
            matcher="doc-builder",
            match_mode="exact",
            effect="allow",
            boundary_scope="any",
            reason="trusted",
        ),
        SafetySkillCallConfig(
            name="elev-deploy",
            matcher="deploy-",
            match_mode="prefix",
            effect="elevated",
            boundary_scope="host",
            reason="needs elevated",
        ),
    ]
    rules = _normalize_user_skill_call_rules(user_cfgs)
    assert len(rules) == 2
    assert all(isinstance(r, SkillCallRule) for r in rules)
    assert rules[0].name == "trust-doc"
    assert rules[0].boundary_scope is BoundaryScope.ANY
    assert rules[0].effect == "allow"
    assert rules[1].boundary_scope is BoundaryScope.HOST
    assert rules[1].match_mode == "prefix"


def test_normalize_user_rules_drops_sandbox() -> None:
    """yaml 层（``SafetySkillCallConfig._reject_sandbox_scope``）已禁
    ``boundary_scope="sandbox"``：构造 yaml 配置应抛 ValidationError，
    因此到 _normalize_user_skill_call_rules 时不会有 sandbox 规则。
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SafetySkillCallConfig(
            name="will-fail",
            matcher="x",
            match_mode="exact",
            effect="allow",
            boundary_scope="sandbox",
        )


# ---------------------------------------------------------------------------
# §5 DEFAULT_SKILL_CALL_RULES 默认空（deny-by-default 锚点）
# ---------------------------------------------------------------------------


def test_default_skill_call_rules_empty() -> None:
    """``DEFAULT_SKILL_CALL_RULES`` 是空 tuple —— deny-by-default 不被无声破坏。"""
    assert DEFAULT_SKILL_CALL_RULES == ()
    assert isinstance(DEFAULT_SKILL_CALL_RULES, tuple)


def test_from_config_with_no_user_rules_yields_default_fallback() -> None:
    """``from_config`` 不传 user rules 时，``_classify_skill`` 走 standard fallback。"""
    resolver = ConsentResolver.from_config(_cfg(), interactive_approval=_NeverApproval())
    hit = _classify(resolver, name="anything")
    assert hit.severity == "standard"
    assert hit.matched_rule == "skill-call-default"
