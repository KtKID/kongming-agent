""":class:`safety.approval.rules.ApprovalRules` 单测（approval-rules-unified 完整重写）。

覆盖 task `approval-rules-unified` 后的新接口：``ApprovalRules(policy=...)`` 注入
:class:`_AutoApprovalPolicyProto`，``classify`` 委托策略并依据
``auto_eligible`` / ``blocked_by_rule`` / ``timeout_ms`` 输出 ``_RuleDecision``。

测试矩阵：

1. ``test_classify_fail_closed_when_policy_none`` —— policy=None → 默认人工审批 + 60s。
2. ``test_classify_returns_default_for_unmanaged_channels`` ——
   claude_code / cron / evolution 通道恒走默认（不调 policy.classify）。
3. ``test_classify_blocked_starts_auto_reject_countdown`` —— 策略返回
   ``blocked_by_rule="bash_rm_any"`` → matched_rule 透传 + auto_reject_at_ms≈now+timeout。
4. ``test_classify_auto_eligible_and_enabled_starts_auto_approve_countdown``
   —— auto_eligible=True + is_enabled_for(cwd)=True → ``auto_approve_at_ms ≈ now+timeout``。
5. ``test_classify_auto_eligible_but_disabled_returns_default`` ——
   auto_eligible=True 但 is_enabled_for(cwd)=False → 默认 ask + 60s。
6. ``test_classify_timeout_ms_le_zero_falls_back_to_60s`` ——
   策略返回 ``timeout_ms=0`` / 负数 → 兜底 60_000（用户硬约束）。
7. ``test_classify_fail_closed_when_policy_raises`` —— policy.classify 抛异常
   → 默认人工审批 + 60s（不向上抛）。
8. ``test_classify_is_elevated_always_false`` —— 阶段 1 generic_chat 永远以
   ``is_elevated=False`` 调 policy.classify（spec 阶段 5 才区分）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from safety.approval.rules import ApprovalRules, _RuleDecision

# ---------------------------------------------------------------------------
# 测试替身：用鸭子类型匹配 _PolicyDecisionLike + _AutoApprovalPolicyProto
# ---------------------------------------------------------------------------


@dataclass
class _FakeDecision:
    """用鸭子类型匹配 :class:`safety.approval.rules._PolicyDecisionLike`。

    字段对齐 :class:`safety.auto_approval.policy.Decision`（仅本层消费 3 字段，
    rule_evaluation 审计快照可省略）。
    """

    auto_eligible: bool
    blocked_by_rule: str | None
    timeout_ms: int


@dataclass
class _FakePolicy:
    """用鸭子类型匹配 :class:`safety.approval.rules._AutoApprovalPolicyProto`。

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
    """``classify`` 抛异常的策略（用例 7 失败关闭验证）。"""

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
# 公共夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def common_input() -> dict[str, Any]:
    """``classify`` 调用的通用参数。"""
    return {
        "thread_id": "thread-test-001",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }


# ---------------------------------------------------------------------------
# 用例 1：policy=None → 失败关闭默认人工审批 + 60s
# ---------------------------------------------------------------------------


def test_classify_fail_closed_when_policy_none(common_input: dict[str, Any]) -> None:
    """policy=None → 任何 channel 都返回默认人工审批 + 60s（失败关闭安全网）。

    覆盖 test 环境 / lifespan 漂移场景，保证审批主流程在配置缺失时仍可阻塞等用户。
    """
    rules = ApprovalRules()  # policy=None
    for channel in ("generic_chat", "claude_code", "cron", "evolution", "cli"):
        dec = rules.classify(channel=channel, cwd="/any/cwd", **common_input)
        assert dec.is_immediate is False
        assert dec.immediate_outcome is None
        assert dec.matched_rule is None
        assert dec.severity == "standard"
        assert dec.auto_approve_at_ms is None, f"channel={channel} 不应自动通过"
        assert dec.auto_reject_at_ms is None
        assert dec.timeout_ms == 60_000


# ---------------------------------------------------------------------------
# 用例 2：未托管通道恒走默认（不调 policy.classify）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["claude_code", "cron", "evolution"])
def test_classify_returns_default_for_unmanaged_channels(
    channel: str,
    common_input: dict[str, Any],
) -> None:
    """未接入审批管理器自动审批的通道恒走默认人工审批 + 60s——claude_code 自走 host_adapter，
    cron / evolution 由各自后续 task 接入；本通道防御性兜底，不让
    "未规划路径" 走策略出意外。

    断言：``policy.classify`` 未被调用（call_count=0）。
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(auto_eligible=True, blocked_by_rule=None, timeout_ms=10_000),
        enabled_cwds={"/any/cwd"},
    )
    rules = ApprovalRules(policy=policy)

    dec = rules.classify(channel=channel, cwd="/any/cwd", **common_input)

    assert dec.auto_approve_at_ms is None, f"channel={channel} 不应自动通过"
    assert dec.matched_rule is None
    assert dec.timeout_ms == 60_000
    # 关键：policy.classify 未被调（防御性兜底，避免未规划通道意外触发）
    assert policy.classify_calls == []


# ---------------------------------------------------------------------------
# 用例 3：blocked_by_rule → 危险待审 + 超时自动拒绝
# ---------------------------------------------------------------------------


def test_classify_blocked_starts_auto_reject_countdown(common_input: dict[str, Any]) -> None:
    """策略返回 blocked_by_rule="bash_rm_any" → 危险待审，并带自动拒绝 deadline。

    生产场景：用户在 Zap ON 的 cwd 下跑 ``Bash(rm -rf /tmp/foo)`` → 命中
    24 规则中 ``bash_rm_any`` → 终端 / Web 展示待审；用户不处理时按规则超时拒绝。
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

    before_ms = int(time.time() * 1000)
    dec = rules.classify(
        channel="generic_chat",
        cwd="/proj/foo",
        thread_id="thread-rm",
        tool_name="Bash",
        tool_input={"command": "rm -rf /tmp/foo"},
    )
    after_ms = int(time.time() * 1000)

    assert dec.matched_rule == "bash_rm_any"
    assert dec.auto_approve_at_ms is None, "阻断规则命中时不得启动自动通过倒计时"
    assert dec.auto_reject_at_ms is not None
    assert before_ms + 10_000 <= dec.auto_reject_at_ms <= after_ms + 10_000 + 1
    assert dec.severity == "elevated"
    assert dec.timeout_ms == 10_000  # 用 policy 返回的 timeout
    # is_elevated=False（阶段 1 generic_chat 都按 False）
    assert policy.classify_calls[0]["is_elevated"] is False


# ---------------------------------------------------------------------------
# 用例 4：auto_eligible + enabled → 自动通过倒计时
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
# 用例 5：auto_eligible 但 cwd 未启用 → 默认人工审批（总开关关闭）
# ---------------------------------------------------------------------------


def test_classify_auto_eligible_but_disabled_returns_default(
    common_input: dict[str, Any],
) -> None:
    """auto_eligible=True 但 ``is_enabled_for(cwd)=False`` → 默认人工审批 + 60s。

    生产场景：用户主动关掉了此 cwd 的总开关（Zap OFF），即使 policy 评估
    "可自动通过" 也保持人工审批。
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
# 用例 6：timeout_ms ≤ 0 → 兜底 60_000
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy_timeout_ms", [0, -1, -100])
def test_classify_timeout_ms_le_zero_falls_back_to_60s(
    policy_timeout_ms: int,
    common_input: dict[str, Any],
) -> None:
    """策略返回 ``timeout_ms ≤ 0`` → 兜底 60_000（用户硬约束）。

    防御性兜底：避免策略配置漂移（如 yaml 写 ``default_timeout_ms: 0``）
    导致审批管理器用 ``actual_timeout_ms <= 0`` 注册超时任务立即触发。
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
# 用例 7：policy.classify 抛异常 → 失败关闭默认人工审批
# ---------------------------------------------------------------------------


def test_classify_fail_closed_when_policy_raises(common_input: dict[str, Any]) -> None:
    """policy.classify 抛异常时失败关闭走默认人工审批 + 60s，**不向上抛**。

    审批主流程不能因配置读取失败而中断（与 manager 用
    ``gather(..., return_exceptions=True)`` 包裹接收器调用同款思路）。
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


# ---------------------------------------------------------------------------
# generic-chat-session-grant：thread 级本次会话同意
# ---------------------------------------------------------------------------


def test_add_session_grant_writes_thread_overrides() -> None:
    """``add_session_grant(generic_chat, ...)`` → 写入 ``_thread_overrides``。

    覆盖 fix-report-20260520-generic-chat-session-grant 核心写入路径：
    用户点弹卡「本次会话都同意」→ ApprovalManager.resolve → 本方法 →
    下次同 thread + cwd + tool classify 命中 immediate allow。
    """
    rules = ApprovalRules()  # 无需 policy（grant 写入与 policy 无关）

    rules.add_session_grant(
        channel="generic_chat",
        thread_id="thread-grant-001",
        cwd="/proj/foo",
        tool_name="Bash",
    )

    assert ("/proj/foo", "Bash") in rules._thread_overrides["thread-grant-001"]
    assert len(rules._thread_overrides["thread-grant-001"]) == 1


def test_add_session_grant_noop_for_unmanaged_channels() -> None:
    """未接入共享自动审批的通道调 ``add_session_grant`` 静默 no-op。

    generic_chat 保留审批管理器线程级授权；CLI 终端只支持单次允许 / 拒绝。
    claude_code 走 WebHostAdapter 直调 policy，cron / evolution 后续 task 接入。
    """
    rules = ApprovalRules()
    for channel in ("cli", "claude_code", "cron", "evolution", "unknown"):
        rules.add_session_grant(
            channel=channel,
            thread_id="thread-x",
            cwd="/any",
            tool_name="Bash",
        )
    assert rules._thread_overrides == {}


def test_add_session_grant_noop_for_cli() -> None:
    """CLI 通道调用 ``add_session_grant`` 静默 no-op。"""
    rules = ApprovalRules()

    rules.add_session_grant(
        channel="cli",
        thread_id="thread-cli",
        cwd="/proj/foo",
        tool_name="run_shell",
    )

    assert rules._thread_overrides == {}


def test_add_session_grant_noop_on_empty_keys() -> None:
    """空 thread_id / cwd / tool_name 都不写入（防御性，避免误反查命中）。"""
    rules = ApprovalRules()
    rules.add_session_grant(channel="generic_chat", thread_id="", cwd="/a", tool_name="B")
    rules.add_session_grant(channel="generic_chat", thread_id="t", cwd="", tool_name="B")
    rules.add_session_grant(channel="generic_chat", thread_id="t", cwd="/a", tool_name="")
    assert rules._thread_overrides == {}


def test_classify_hits_thread_grant_returns_immediate_allow() -> None:
    """线程级授权命中 + 策略允许 → ``is_immediate=True`` + ``immediate_outcome='approved'``。

    生产场景：用户上次审批 Bash 时点了「本次会话都同意」→ 本 thread 下次
    再 Bash → ApprovalRules.classify 命中 ``_thread_overrides`` → manager
    直接走 ``is_immediate`` 分支返 approved，不创建 pending。
    """
    # 这里策略允许（非 blocked，auto_eligible 不影响授权命中分支）
    policy = _FakePolicy(
        next_decision=_FakeDecision(auto_eligible=False, blocked_by_rule=None, timeout_ms=10_000),
        enabled_cwds=set(),
    )
    rules = ApprovalRules(policy=policy)
    rules.add_session_grant(
        channel="generic_chat",
        thread_id="thread-allow",
        cwd="/proj/foo",
        tool_name="Bash",
    )

    dec = rules.classify(
        channel="generic_chat",
        thread_id="thread-allow",
        cwd="/proj/foo",
        tool_name="Bash",
        tool_input={"command": "ls"},
    )

    assert dec.is_immediate is True
    assert dec.immediate_outcome == "approved"
    assert dec.matched_rule is None


def test_classify_thread_grant_isolation_per_thread() -> None:
    """thread A 的授权不影响 thread B（多标签页 / 多会话隔离保证）。"""
    policy = _FakePolicy(
        next_decision=_FakeDecision(auto_eligible=False, blocked_by_rule=None, timeout_ms=10_000),
        enabled_cwds=set(),
    )
    rules = ApprovalRules(policy=policy)
    rules.add_session_grant(
        channel="generic_chat",
        thread_id="thread-A",
        cwd="/proj/foo",
        tool_name="Bash",
    )

    # thread A 命中
    dec_a = rules.classify(
        channel="generic_chat",
        thread_id="thread-A",
        cwd="/proj/foo",
        tool_name="Bash",
        tool_input={"command": "ls"},
    )
    assert dec_a.is_immediate is True

    # thread B 同样 cwd + tool 不应命中（隔离）
    dec_b = rules.classify(
        channel="generic_chat",
        thread_id="thread-B",
        cwd="/proj/foo",
        tool_name="Bash",
        tool_input={"command": "ls"},
    )
    assert dec_b.is_immediate is False
    assert dec_b.immediate_outcome is None


def test_classify_thread_grant_isolation_per_cwd_and_tool() -> None:
    """(cwd, tool_name) 元组完全匹配才命中；改任一字段不命中。"""
    policy = _FakePolicy(
        next_decision=_FakeDecision(auto_eligible=False, blocked_by_rule=None, timeout_ms=10_000),
        enabled_cwds=set(),
    )
    rules = ApprovalRules(policy=policy)
    rules.add_session_grant(
        channel="generic_chat",
        thread_id="thread-x",
        cwd="/proj/foo",
        tool_name="Bash",
    )

    # 改 cwd 不命中
    dec_cwd = rules.classify(
        channel="generic_chat",
        thread_id="thread-x",
        cwd="/proj/bar",
        tool_name="Bash",
        tool_input={"command": "ls"},
    )
    assert dec_cwd.is_immediate is False

    # 改 tool 不命中
    dec_tool = rules.classify(
        channel="generic_chat",
        thread_id="thread-x",
        cwd="/proj/foo",
        tool_name="Edit",
        tool_input={"path": "/proj/foo/a"},
    )
    assert dec_tool.is_immediate is False


def test_classify_thread_grant_blocked_by_rule_starts_auto_reject() -> None:
    """**关键安全证据**：线程级授权命中 + policy.blocked_by_rule → 超时自动拒绝。

    防御 rm 等危险规则被本次会话授权绕过——用户即使对 Bash 点了
    「本次会话都同意」，下次跑 ``rm -rf`` 仍命中 ``bash_rm_any``
    进入待审，并带自动拒绝 deadline。
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(
            auto_eligible=False,
            blocked_by_rule="bash_rm_any",
            timeout_ms=15_000,
        ),
        enabled_cwds={"/proj/danger"},
    )
    rules = ApprovalRules(policy=policy)
    rules.add_session_grant(
        channel="generic_chat",
        thread_id="thread-danger",
        cwd="/proj/danger",
        tool_name="Bash",
    )

    before_ms = int(time.time() * 1000)
    dec = rules.classify(
        channel="generic_chat",
        thread_id="thread-danger",
        cwd="/proj/danger",
        tool_name="Bash",
        tool_input={"command": "rm -rf /tmp/some"},
    )
    after_ms = int(time.time() * 1000)

    # 关键断言：危险规则优先级最高，本次会话授权不放行
    assert dec.is_immediate is False, (
        "本次会话授权命中后，命中 blocked_by_rule 必须进入待审；"
        "rm 之类危险命令需要继续受自动拒绝 deadline 保护"
    )
    assert dec.immediate_outcome is None
    assert dec.matched_rule == "bash_rm_any"
    assert dec.auto_approve_at_ms is None
    assert dec.auto_reject_at_ms is not None
    assert before_ms + 15_000 <= dec.auto_reject_at_ms <= after_ms + 15_000 + 1
    assert dec.severity == "elevated"
    assert dec.timeout_ms == 15_000  # 用 policy 返回的 timeout


def test_classify_thread_grant_fails_closed_when_policy_none() -> None:
    """policy=None（失败关闭配置缺失）+ 授权命中 → 默认人工审批。

    没有策略就无法检查 blocked_by_rule；本次会话授权不能在这种状态下
    直接放行。
    """
    rules = ApprovalRules()  # policy=None
    rules.add_session_grant(
        channel="generic_chat",
        thread_id="thread-noop",
        cwd="/proj/foo",
        tool_name="Bash",
    )

    dec = rules.classify(
        channel="generic_chat",
        thread_id="thread-noop",
        cwd="/proj/foo",
        tool_name="Bash",
        tool_input={"command": "ls"},
    )

    assert dec.is_immediate is False
    assert dec.immediate_outcome is None
    assert dec.timeout_ms == 60_000


def test_classify_thread_grant_applies_to_managed_channels_only() -> None:
    """线程级授权写入 _thread_overrides 后，仅 generic_chat 查得到。

    CLI 接入共享自动审批规则，但终端交互不使用 session grant。
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(auto_eligible=False, blocked_by_rule=None, timeout_ms=10_000),
        enabled_cwds=set(),
    )
    rules = ApprovalRules(policy=policy)
    rules._thread_overrides.setdefault("thread-x", set()).add(("/proj/foo", "Bash"))

    dec_generic = rules.classify(
        channel="generic_chat",
        thread_id="thread-x",
        cwd="/proj/foo",
        tool_name="Bash",
        tool_input={"command": "ls"},
    )
    assert dec_generic.is_immediate is True
    assert dec_generic.immediate_outcome == "approved"

    for channel in ("cli", "claude_code", "cron", "evolution"):
        dec = rules.classify(
            channel=channel,
            thread_id="thread-x",
            cwd="/proj/foo",
            tool_name="Bash",
            tool_input={"command": "ls"},
        )
        assert dec.is_immediate is False, f"channel={channel} 不应命中 manager grant"
        assert dec.immediate_outcome is None


def test_clear_thread_grants_removes_all_for_thread() -> None:
    """``clear_thread_grants(thread_id)`` 清掉该 thread 全部 (cwd, tool) 条目，返计数。"""
    rules = ApprovalRules()
    rules.add_session_grant(
        channel="generic_chat", thread_id="thread-A", cwd="/a", tool_name="Bash"
    )
    rules.add_session_grant(
        channel="generic_chat", thread_id="thread-A", cwd="/b", tool_name="Edit"
    )
    rules.add_session_grant(
        channel="generic_chat", thread_id="thread-B", cwd="/c", tool_name="Bash"
    )

    n = rules.clear_thread_grants("thread-A")
    assert n == 2
    assert "thread-A" not in rules._thread_overrides
    # 其他 thread 不受影响
    assert ("/c", "Bash") in rules._thread_overrides["thread-B"]


def test_clear_thread_grants_returns_zero_when_thread_unknown() -> None:
    """thread_id 不在 overrides 中 → 返 0（幂等）。"""
    rules = ApprovalRules()
    assert rules.clear_thread_grants("unknown-thread") == 0
