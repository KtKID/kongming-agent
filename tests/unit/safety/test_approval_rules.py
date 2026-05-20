""":class:`safety.approval_rules.ApprovalRules` 单测（approval-rules-unified 完整重写）。

覆盖 task `approval-rules-unified` 后的新接口：``ApprovalRules(policy=...)`` 注入
:class:`_AutoApprovalPolicyProto`，``classify`` 委托 policy 并依据
``auto_eligible`` / ``blocked_by_rule`` / ``timeout_ms`` 输出 ``_RuleDecision``。

测试矩阵：

1. ``test_classify_fail_closed_when_policy_none`` —— policy=None → 默认 ask + 60s。
2. ``test_classify_returns_default_for_non_generic_chat_channels`` ——
   claude_code / cron / evolution / cli 通道恒走默认（不调 policy.classify）。
3. ``test_classify_blocked_forces_human_approval`` —— policy 返
   ``blocked_by_rule="bash_rm_any"`` → matched_rule 透传 + auto_approve_at_ms=None。
4. ``test_classify_auto_eligible_and_enabled_starts_auto_approve_countdown``
   —— auto_eligible=True + is_enabled_for(cwd)=True → ``auto_approve_at_ms ≈ now+timeout``。
5. ``test_classify_auto_eligible_but_disabled_returns_default`` ——
   auto_eligible=True 但 is_enabled_for(cwd)=False → 默认 ask + 60s。
6. ``test_classify_timeout_ms_le_zero_falls_back_to_60s`` ——
   policy 返 ``timeout_ms=0`` / 负数 → fallback 60_000（用户硬约束）。
7. ``test_classify_fail_closed_when_policy_raises`` —— policy.classify 抛异常
   → 默认 ask + 60s（不向上抛）。
8. ``test_classify_is_elevated_always_false`` —— 阶段 1 generic_chat 永远以
   ``is_elevated=False`` 调 policy.classify（spec 阶段 5 才区分）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from safety.approval_rules import ApprovalRules, _RuleDecision

# ---------------------------------------------------------------------------
# 测试用 fake：duck typing 匹配 _PolicyDecisionLike + _AutoApprovalPolicyProto
# ---------------------------------------------------------------------------


@dataclass
class _FakeDecision:
    """duck typing 匹配 :class:`safety.approval_rules._PolicyDecisionLike`。

    字段对齐 :class:`web.auto_approval.policy.Decision`（仅本层消费 3 字段，
    rule_evaluation audit 快照可省略）。
    """

    auto_eligible: bool
    blocked_by_rule: str | None
    timeout_ms: int


@dataclass
class _FakePolicy:
    """duck typing 匹配 :class:`safety.approval_rules._AutoApprovalPolicyProto`。

    构造时配置 ``next_decision`` + ``enabled_cwds``（按 cwd 枚举开关）；
    ``classify_calls`` 记录全部入参用于断言调用契约（is_elevated / cwd / tool）。
    """

    next_decision: _FakeDecision
    enabled_cwds: set[str] = field(default_factory=set)
    classify_calls: list[dict[str, Any]] = field(default_factory=list)

    def classify(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str,
        is_elevated: bool,
    ) -> _FakeDecision:
        self.classify_calls.append(
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "cwd": cwd,
                "is_elevated": is_elevated,
            },
        )
        return self.next_decision

    def is_enabled_for(self, cwd: str) -> bool:
        return cwd in self.enabled_cwds


class _RaisingPolicy:
    """``classify`` 抛异常的 policy（用例 7 fail-closed 验证）。"""

    def classify(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str,
        is_elevated: bool,
    ) -> Any:
        raise RuntimeError(f"simulated policy error for cwd={cwd}, tool={tool_name}")

    def is_enabled_for(self, cwd: str) -> bool:  # pragma: no cover - 不会被调到
        return False


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def common_input() -> dict[str, Any]:
    """classify 调用的通用参数。"""
    return {
        "thread_id": "thread-test-001",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }


# ---------------------------------------------------------------------------
# 用例 1：policy=None → fail-closed 默认 ask + 60s
# ---------------------------------------------------------------------------


def test_classify_fail_closed_when_policy_none(common_input: dict[str, Any]) -> None:
    """policy=None → 任何 channel 都返回默认 ask + 60s（fail-closed 安全网）。

    覆盖 test 环境 / lifespan 漂移场景，保证审批主流程在配置缺失时仍可阻塞等用户。
    """
    rules = ApprovalRules()  # policy=None
    for channel in ("generic_chat", "claude_code", "cron", "evolution", "cli"):
        dec = rules.classify(channel=channel, cwd="/any/cwd", **common_input)
        assert dec.is_immediate is False
        assert dec.immediate_outcome is None
        assert dec.matched_rule is None
        assert dec.severity == "standard"
        assert dec.auto_approve_at_ms is None, f"channel={channel} should not auto-approve"
        assert dec.auto_reject_at_ms is None
        assert dec.timeout_ms == 60_000


# ---------------------------------------------------------------------------
# 用例 2：非 generic_chat 通道恒走默认（不调 policy.classify）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["claude_code", "cron", "evolution", "cli"])
def test_classify_returns_default_for_non_generic_chat_channels(
    channel: str,
    common_input: dict[str, Any],
) -> None:
    """非 generic_chat 通道恒走默认 ask + 60s——claude_code 自走 host_adapter，
    cron / evolution / cli 由各自后续 task 接入；本通道防御性兜底，不让
    "未规划路径" 走 policy 出意外。

    断言：``policy.classify`` 未被调用（call_count=0）。
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(auto_eligible=True, blocked_by_rule=None, timeout_ms=10_000),
        enabled_cwds={"/any/cwd"},
    )
    rules = ApprovalRules(policy=policy)

    dec = rules.classify(channel=channel, cwd="/any/cwd", **common_input)

    assert dec.auto_approve_at_ms is None, f"channel={channel} should not auto-approve"
    assert dec.matched_rule is None
    assert dec.timeout_ms == 60_000
    # 关键：policy.classify 未被调（防御性兜底，避免未规划通道意外触发）
    assert policy.classify_calls == []


# ---------------------------------------------------------------------------
# 用例 3：blocked_by_rule → 强制人审（matched_rule 透传，不启 auto-approve）
# ---------------------------------------------------------------------------


def test_classify_blocked_forces_human_approval(common_input: dict[str, Any]) -> None:
    """policy 返 blocked_by_rule="bash_rm_any" → 强制人审，``auto_approve_at_ms=None``。

    生产场景：用户在 Zap ON 的 cwd 下跑 ``Bash(rm -rf /tmp/foo)`` → 命中
    24 规则中 ``bash_rm_any`` → 即使总开关 ON 也必须人审（守护危险操作）。
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(
            auto_eligible=False,
            blocked_by_rule="bash_rm_any",
            timeout_ms=10_000,
        ),
        enabled_cwds={"/proj/foo"},  # cwd 启用，但规则命中优先
    )
    rules = ApprovalRules(policy=policy)

    dec = rules.classify(
        channel="generic_chat",
        cwd="/proj/foo",
        thread_id="thread-rm",
        tool_name="Bash",
        tool_input={"command": "rm -rf /tmp/foo"},
    )

    assert dec.matched_rule == "bash_rm_any"
    assert dec.auto_approve_at_ms is None, "blocked rule must NOT start auto-approve countdown"
    assert dec.auto_reject_at_ms is None
    assert dec.severity == "standard"
    assert dec.timeout_ms == 10_000  # 用 policy 返回的 timeout
    # is_elevated=False（阶段 1 generic_chat 都按 False）
    assert policy.classify_calls[0]["is_elevated"] is False


# ---------------------------------------------------------------------------
# 用例 4：auto_eligible + enabled → auto-approve 倒计时
# ---------------------------------------------------------------------------


def test_classify_auto_eligible_and_enabled_starts_auto_approve_countdown(
    common_input: dict[str, Any],
) -> None:
    """auto_eligible=True + is_enabled_for(cwd)=True → ``auto_approve_at_ms ≈ now+timeout``。

    生产场景：用户在 Zap ON 的 cwd 下跑 ``Bash(ls)`` → 未命中任何危险规则 →
    倒计时自动通过（10s）。
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(
            auto_eligible=True,
            blocked_by_rule=None,
            timeout_ms=10_000,
        ),
        enabled_cwds={"/proj/foo"},
    )
    rules = ApprovalRules(policy=policy)

    before_ms = int(time.time() * 1000)
    dec = rules.classify(channel="generic_chat", cwd="/proj/foo", **common_input)
    after_ms = int(time.time() * 1000)

    assert dec.matched_rule is None
    assert dec.auto_reject_at_ms is None
    assert dec.severity == "standard"
    assert dec.timeout_ms == 10_000
    assert dec.auto_approve_at_ms is not None
    # 容差：now + 10000 ± (after_ms - before_ms + 1ms)
    assert before_ms + 10_000 <= dec.auto_approve_at_ms <= after_ms + 10_000 + 1


# ---------------------------------------------------------------------------
# 用例 5：auto_eligible 但 cwd disabled → 默认 ask（Zap OFF）
# ---------------------------------------------------------------------------


def test_classify_auto_eligible_but_disabled_returns_default(
    common_input: dict[str, Any],
) -> None:
    """auto_eligible=True 但 ``is_enabled_for(cwd)=False`` → 默认 ask + 60s。

    生产场景：用户主动关掉了此 cwd 的总开关（Zap OFF），即使 policy 评估
    "可自动通过" 也保持 ask。
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(
            auto_eligible=True,
            blocked_by_rule=None,
            timeout_ms=10_000,
        ),
        enabled_cwds=set(),  # 没有任何 cwd 启用
    )
    rules = ApprovalRules(policy=policy)

    dec = rules.classify(channel="generic_chat", cwd="/proj/foo", **common_input)

    assert dec.auto_approve_at_ms is None
    assert dec.auto_reject_at_ms is None
    assert dec.matched_rule is None
    assert dec.timeout_ms == 60_000  # 走 fail-closed 默认（不是 policy.timeout_ms）


# ---------------------------------------------------------------------------
# 用例 6：timeout_ms ≤ 0 → fallback 60_000
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy_timeout_ms", [0, -1, -100])
def test_classify_timeout_ms_le_zero_falls_back_to_60s(
    policy_timeout_ms: int,
    common_input: dict[str, Any],
) -> None:
    """policy 返 ``timeout_ms ≤ 0`` → fallback 60_000（用户硬约束）。

    防御性兜底：避免 policy 配置漂移（如 yaml 写 ``default_timeout_ms: 0``）
    导致 manager 用 ``actual_timeout_ms <= 0`` 注册 timeout task 立即触发。
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(
            auto_eligible=True,
            blocked_by_rule=None,
            timeout_ms=policy_timeout_ms,
        ),
        enabled_cwds={"/proj/foo"},
    )
    rules = ApprovalRules(policy=policy)

    before_ms = int(time.time() * 1000)
    dec = rules.classify(channel="generic_chat", cwd="/proj/foo", **common_input)
    after_ms = int(time.time() * 1000)

    assert dec.timeout_ms == 60_000
    assert dec.auto_approve_at_ms is not None
    assert before_ms + 60_000 <= dec.auto_approve_at_ms <= after_ms + 60_000 + 1


# ---------------------------------------------------------------------------
# 用例 7：policy.classify 抛异常 → fail-closed 默认 ask
# ---------------------------------------------------------------------------


def test_classify_fail_closed_when_policy_raises(common_input: dict[str, Any]) -> None:
    """policy.classify 抛异常时 fail-closed 走默认 ask + 60s，**不向上抛**。

    审批主流程不能因配置读取失败而中断（与 manager 用
    ``gather(..., return_exceptions=True)`` 包裹 sink 调用同款思路）。
    """
    rules = ApprovalRules(policy=_RaisingPolicy())

    # 关键断言：调用本身不抛异常
    dec = rules.classify(channel="generic_chat", cwd="/proj/foo", **common_input)

    assert dec.auto_approve_at_ms is None
    assert dec.auto_reject_at_ms is None
    assert dec.matched_rule is None
    assert dec.timeout_ms == 60_000
    assert dec.severity == "standard"
    assert isinstance(dec, _RuleDecision)


# ---------------------------------------------------------------------------
# 用例 8：is_elevated 阶段 1 永远 False
# ---------------------------------------------------------------------------


def test_classify_is_elevated_always_false(common_input: dict[str, Any]) -> None:
    """ApprovalRules 阶段 1 调 policy.classify 时 ``is_elevated`` 永远传 ``False``。

    spec 阶段 5 才区分 elevated（codex / cli 接入时引入）；阶段 1 generic_chat
    没有 elevated 概念，不能误传 True 让 policy 走 elevated 硬约束路径。
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(auto_eligible=True, blocked_by_rule=None, timeout_ms=5_000),
        enabled_cwds={"/proj/foo"},
    )
    rules = ApprovalRules(policy=policy)
    rules.classify(channel="generic_chat", cwd="/proj/foo", **common_input)

    assert len(policy.classify_calls) == 1
    assert policy.classify_calls[0]["is_elevated"] is False
    # cwd / tool 字段也透传（断言调用契约）
    assert policy.classify_calls[0]["cwd"] == "/proj/foo"
    assert policy.classify_calls[0]["tool_name"] == "Bash"
    assert policy.classify_calls[0]["tool_input"] == {"command": "ls"}
