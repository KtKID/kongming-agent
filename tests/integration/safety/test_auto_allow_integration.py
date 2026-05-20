"""generic_chat per-cwd 自动通过倒计时端到端集成测（approval-rules-unified 重构）。

覆盖 :class:`safety.approval_manager.ApprovalManager._handle_auto_approve` 的
完整生命周期 + 三退出路径 cancel 防护 + race fail-safe + rm 危险规则保护：

1. ``test_auto_approve_resolves_when_cwd_enabled`` ——
   policy 返 auto_eligible=True + cwd enabled + 短 timeout → manager.request
   阻塞 → 等 delay → future 自动 set_result(approved + source='rule_auto_allow')。

2. ``test_auto_approve_does_not_start_when_cwd_disabled`` ——
   policy 返 auto_eligible=True 但 ``is_enabled_for(cwd)=False`` →
   不创建 auto_approve_task；保持原 ask + 60s 行为。

3. ``test_user_resolve_cancels_auto_approve_task`` ——
   启 auto-approve timer（delay 充足）→ 用户在 timer 触发前 ``manager.resolve``
   → request() finally 走 ``_cleanup_pending`` → 同步 cancel auto-approve task。

4. ``test_manager_cancel_cancels_auto_approve_task`` ——
   启 auto-approve timer → ``manager.cancel`` → cleanup cancel auto-approve task。

5. ``test_cancel_by_thread_cancels_auto_approve_task`` ——
   启 auto-approve timer → cancel_by_thread（cell evict 路径）→ cleanup
   cancel auto-approve task。

6. ``test_race_safe_when_user_resolve_and_auto_approve_fire_simultaneously`` ——
   极短 auto-approve delay 与 user resolve 同时触发的 race：
   future 只被 set_result 一次（由 future.done() 守卫），不抛 InvalidStateError。

7. ``test_policy_shared_across_channels`` ——
   关键证据：装配时 ``ApprovalRules._policy is policy``
   是同一份对象引用；UI 写盘后 ``ApprovalRules.classify`` 立即读到新值
   （ConfigStore 不缓存）——"开关一处管所有通道" 的架构保证。

8. ``test_real_policy_drives_manager_auto_approve`` ——
   端到端 happy path：真实 ``AutoApprovalPolicy`` + ``ConfigStore`` →
   manager.request 自动 resolve approved（验证 duck typing 不漂移）。

9. ``test_bash_rm_blocked_forces_human_approval_even_when_zap_on`` ——
   approval-rules-unified DoD 关键证据：generic_chat + Bash(rm) + Zap ON →
   走真实 24 规则 → blocked_by_rule="bash_rm_any" → 强制人审，不启 auto-approve。

模拟 ws / 不实际启 FastAPI TestClient：集中验证 manager + ApprovalRules +
AutoApprovalPolicy 三件套的端到端集成。
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from core.contracts import ApprovalDecision
from safety.approval_manager import (
    ApprovalManager,
    reset_for_testing,
)
from safety.approval_rules import ApprovalRules
from web.auto_approval.config_store import ConfigStore, ProjectConfig

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fixtures / 小工具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """每用例隔离 manager 单例，避免组合跑相互污染。"""
    reset_for_testing()
    yield
    reset_for_testing()


@dataclass
class _FakeDecision:
    """duck typing 匹配 :class:`safety.approval_rules._PolicyDecisionLike`。"""

    auto_eligible: bool
    blocked_by_rule: str | None
    timeout_ms: int


@dataclass
class _FakePolicy:
    """duck typing 匹配 :class:`safety.approval_rules._AutoApprovalPolicyProto`。

    构造时注入静态 decision + cwd 启用集合；满足集成测用例 1-6 + race fail-safe
    场景需求（不依赖完整 RuleSet 加载）。
    """

    next_decision: _FakeDecision
    enabled_cwds: set[str] = field(default_factory=set)

    def classify(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str,
        is_elevated: bool,
    ) -> _FakeDecision:
        return self.next_decision

    def is_enabled_for(self, cwd: str) -> bool:
        return cwd in self.enabled_cwds


def _make_manager_with_policy(
    *,
    policy: _FakePolicy | Any | None,
    default_timeout_ms: int = 10_000,
) -> ApprovalManager:
    """构造裸 manager 实例（注入 ApprovalRules + 可选 policy）。"""
    return ApprovalManager(
        rules=ApprovalRules(policy=policy),
        default_timeout_ms=default_timeout_ms,
    )


# ---------------------------------------------------------------------------
# 用例 1：cwd enabled=True → 自动 resolve
# ---------------------------------------------------------------------------


async def test_auto_approve_resolves_when_cwd_enabled() -> None:
    """generic_chat + cwd enabled=True + 短 timeout → request 自动 set_result(approved)。

    断言：
    - decision.outcome == 'approved'
    - decision.metadata.source == 'rule_auto_allow'
    - R10：pending / timeout_tasks / auto_approve_tasks 全清空
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(auto_eligible=True, blocked_by_rule=None, timeout_ms=100),
        enabled_cwds={"/proj/foo"},
    )
    manager = _make_manager_with_policy(policy=policy, default_timeout_ms=10_000)

    decision = await manager.request(
        channel="generic_chat",
        thread_id="t-auto",
        cwd="/proj/foo",
        tool_name="Bash",
        tool_input={"cmd": "ls"},
        timeout_ms=5_000,
    )
    assert decision.outcome == "approved"
    assert decision.metadata.get("source") == "rule_auto_allow"
    assert decision.metadata.get("reason") == "auto_allow"
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0
    assert manager.auto_approve_task_count == 0


# ---------------------------------------------------------------------------
# 用例 2：cwd disabled → 不启 auto-approve task
# ---------------------------------------------------------------------------


async def test_auto_approve_does_not_start_when_cwd_disabled() -> None:
    """policy 返 auto_eligible=True 但 cwd disabled → 不创建 auto-approve task；
    request 仍走原 ask 路径（必须等 timeout / cancel 才返回）。

    用极短 default_timeout_ms 触发 fail-closed timeout，断言：
    - decision.source == 'manager_timeout'
    - 中途 ``auto_approve_task_count == 0``
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(auto_eligible=True, blocked_by_rule=None, timeout_ms=100),
        enabled_cwds=set(),  # cwd 未启用
    )
    manager = _make_manager_with_policy(policy=policy, default_timeout_ms=100)

    request_task = asyncio.create_task(
        manager.request(
            channel="generic_chat",
            thread_id="t-disabled",
            cwd="/proj/foo",
            tool_name="Bash",
            tool_input={"cmd": "ls"},
        )
    )
    for _ in range(50):
        if manager.pending_count > 0:
            break
        await asyncio.sleep(0.005)
    assert manager.auto_approve_task_count == 0
    decision = await request_task
    assert decision.outcome == "rejected"
    assert decision.metadata.get("source") == "manager_timeout"
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0
    assert manager.auto_approve_task_count == 0


# ---------------------------------------------------------------------------
# 用例 3：user resolve 提前触发 → cleanup cancel auto-approve task
# ---------------------------------------------------------------------------


async def test_user_resolve_cancels_auto_approve_task() -> None:
    """启 auto-approve timer (delay=10s) → 用户在 timer 触发前 resolve → cleanup
    同步 cancel auto-approve task。
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(auto_eligible=True, blocked_by_rule=None, timeout_ms=10_000),
        enabled_cwds={"/proj/foo"},
    )
    manager = _make_manager_with_policy(policy=policy, default_timeout_ms=60_000)

    async def user_resolve_quickly() -> None:
        for _ in range(50):
            if manager.pending_count > 0 and manager.auto_approve_task_count > 0:
                break
            await asyncio.sleep(0.005)
        assert manager.auto_approve_task_count == 1, (
            "auto_approve task should be started when cwd enabled"
        )
        req_id = next(iter(manager._pending.keys()))
        assert manager.resolve(req_id, {"allow": True})

    resolve_task = asyncio.create_task(user_resolve_quickly())
    decision = await manager.request(
        channel="generic_chat",
        thread_id="t-user-wins",
        cwd="/proj/foo",
        tool_name="Bash",
        tool_input={"cmd": "ls"},
    )
    await resolve_task

    assert decision.outcome == "approved"
    assert decision.metadata.get("source") == "user"
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0
    assert manager.auto_approve_task_count == 0


# ---------------------------------------------------------------------------
# 用例 4：manager.cancel 触发 → cleanup cancel auto-approve task
# ---------------------------------------------------------------------------


async def test_manager_cancel_cancels_auto_approve_task() -> None:
    """启 auto-approve timer → manager.cancel → cleanup cancel auto-approve task。"""
    policy = _FakePolicy(
        next_decision=_FakeDecision(auto_eligible=True, blocked_by_rule=None, timeout_ms=10_000),
        enabled_cwds={"/proj/foo"},
    )
    manager = _make_manager_with_policy(policy=policy, default_timeout_ms=60_000)

    async def cancel_quickly() -> None:
        for _ in range(50):
            if manager.pending_count > 0 and manager.auto_approve_task_count > 0:
                break
            await asyncio.sleep(0.005)
        req_id = next(iter(manager._pending.keys()))
        assert manager.cancel(req_id, reason="test_cancel")

    cancel_task = asyncio.create_task(cancel_quickly())
    decision = await manager.request(
        channel="generic_chat",
        thread_id="t-cancel",
        cwd="/proj/foo",
        tool_name="Bash",
        tool_input={"cmd": "ls"},
    )
    await cancel_task

    assert decision.outcome == "rejected"
    assert decision.metadata.get("source") == "manager_cancel"
    assert decision.metadata.get("reason") == "test_cancel"
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0
    assert manager.auto_approve_task_count == 0


# ---------------------------------------------------------------------------
# 用例 5：cancel_by_thread → cleanup cancel auto-approve task
# ---------------------------------------------------------------------------


async def test_cancel_by_thread_cancels_auto_approve_task() -> None:
    """启 auto-approve timer → cancel_by_thread（cell evict 路径）→ cleanup
    cancel auto-approve task。
    """
    policy = _FakePolicy(
        next_decision=_FakeDecision(auto_eligible=True, blocked_by_rule=None, timeout_ms=10_000),
        enabled_cwds={"/proj/foo"},
    )
    manager = _make_manager_with_policy(policy=policy, default_timeout_ms=60_000)

    async def evict_thread() -> None:
        for _ in range(50):
            if manager.pending_count > 0 and manager.auto_approve_task_count > 0:
                break
            await asyncio.sleep(0.005)
        assert manager.cancel_by_thread("t-evict", reason="cell_evict") >= 1

    evict_task = asyncio.create_task(evict_thread())
    decision = await manager.request(
        channel="generic_chat",
        thread_id="t-evict",
        cwd="/proj/foo",
        tool_name="Bash",
        tool_input={"cmd": "ls"},
    )
    await evict_task

    assert decision.outcome == "rejected"
    assert decision.metadata.get("reason") == "cell_evict"
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0
    assert manager.auto_approve_task_count == 0


# ---------------------------------------------------------------------------
# 用例 6：race fail-safe — user resolve + auto-approve 同时触发
# ---------------------------------------------------------------------------


async def test_race_safe_when_user_resolve_and_auto_approve_fire_simultaneously() -> None:
    """auto-approve delay 极短（10ms）+ user resolve 同时窗口 → future 只 set 一次。

    保护点：:meth:`ApprovalManager._handle_auto_approve` 在 ``asyncio.sleep`` 醒
    来后 **必查 ``pending.future.done()``**——若 user 已先 set，本方法 return；
    若 auto-approve 先 set，user resolve 路径走 ``resolve()`` 内的 ``future.done()``
    守卫，``return False``。两侧 fail-safe，**不抛 InvalidStateError**。
    """
    for _ in range(10):
        policy = _FakePolicy(
            next_decision=_FakeDecision(auto_eligible=True, blocked_by_rule=None, timeout_ms=10),
            enabled_cwds={"/proj/foo"},
        )
        manager = _make_manager_with_policy(policy=policy, default_timeout_ms=60_000)

        async def aggressive_user_resolve(mgr: ApprovalManager) -> None:
            for _ in range(20):
                if mgr.pending_count > 0:
                    break
                await asyncio.sleep(0.001)
            keys = list(mgr._pending.keys())
            if keys:
                mgr.resolve(keys[0], {"allow": True})

        resolve_task = asyncio.create_task(aggressive_user_resolve(manager))
        decision = await manager.request(
            channel="generic_chat",
            thread_id="t-race",
            cwd="/proj/foo",
            tool_name="Bash",
            tool_input={"cmd": "ls"},
        )
        await resolve_task

        assert decision.outcome == "approved"
        assert manager.pending_count == 0
        assert manager.timeout_task_count == 0
        assert manager.auto_approve_task_count == 0


# ---------------------------------------------------------------------------
# 用例 7：架构验证 — 同一份 policy 跨 channel 共享
# ---------------------------------------------------------------------------


async def test_policy_shared_across_channels() -> None:
    """关键证据：generic_chat 的 ApprovalRules 与 claude_code 路径用 **同一份**
    ``AutoApprovalPolicy`` 实例 → UI 一处 toggle 即时生效于所有通道。

    构造真实 ConfigStore + AutoApprovalPolicy（claude_code 路径用），断言：

    1. ``ApprovalRules._policy is policy`` —— 同一份对象引用（架构契约）
    2. ``policy.set_enabled(cwd, True)`` 写盘 → ``ApprovalRules.classify(...)``
       立即读到新值（ConfigStore 不缓存，每次 ``get`` 读盘；保证多通道一致性）。
    """
    from web.auto_approval.policy import AutoApprovalPolicy
    from web.auto_approval.rules import RuleSet

    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ConfigStore(Path(tmp_dir))
        rule_set = RuleSet(version=1, default_timeout_ms=10_000, rules=())
        policy = AutoApprovalPolicy(rule_set, store)

        rules = ApprovalRules(policy=policy)

        # 证据 1：ApprovalRules 持的就是同一份 policy
        assert rules._policy is policy, (
            "ApprovalRules._policy must be the same AutoApprovalPolicy instance "
            "(architectural contract: single source of truth for rules + cwd config)"
        )

        # 证据 2：初始 cwd 未配置 → classify 默认 ask
        dec1 = rules.classify(
            channel="generic_chat",
            thread_id="t",
            cwd="/proj/shared",
            tool_name="Bash",
            tool_input={},
        )
        assert dec1.auto_approve_at_ms is None

        # UI 等价操作：通过 policy.set_enabled 写盘
        policy.set_enabled("/proj/shared", True)

        # 立即查：generic_chat 路径已经读到新配置（不缓存）
        dec2 = rules.classify(
            channel="generic_chat",
            thread_id="t",
            cwd="/proj/shared",
            tool_name="Bash",
            tool_input={"cmd": "ls"},
        )
        assert dec2.auto_approve_at_ms is not None, (
            "shared policy must propagate UI toggle to generic_chat channel"
        )

        # 反向：toggle off 也立即生效
        policy.set_enabled("/proj/shared", False)
        dec3 = rules.classify(
            channel="generic_chat",
            thread_id="t",
            cwd="/proj/shared",
            tool_name="Bash",
            tool_input={"cmd": "ls"},
        )
        assert dec3.auto_approve_at_ms is None


# ---------------------------------------------------------------------------
# 用例 8：真实 AutoApprovalPolicy 端到端 → manager auto-resolve
# ---------------------------------------------------------------------------


async def test_real_policy_drives_manager_auto_approve() -> None:
    """端到端 happy path：真实 AutoApprovalPolicy + ConfigStore 写一条
    enabled=True + 短 timeout → manager.request 自动 resolve approved。

    与用例 1 的差异：用真实 AutoApprovalPolicy 而非 _FakePolicy，验证
    duck typing 在生产对象上确实匹配 _AutoApprovalPolicyProto（避免 Protocol 漂移）。
    """
    from web.auto_approval.policy import AutoApprovalPolicy
    from web.auto_approval.rules import RuleSet

    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ConfigStore(Path(tmp_dir))
        store.set(ProjectConfig(cwd="/proj/real", enabled=True, timeout_ms=200))
        rule_set = RuleSet(version=1, default_timeout_ms=10_000, rules=())
        policy = AutoApprovalPolicy(rule_set, store)

        manager = _make_manager_with_policy(policy=policy, default_timeout_ms=10_000)

        decision = await manager.request(
            channel="generic_chat",
            thread_id="t-real",
            cwd="/proj/real",
            tool_name="Bash",
            tool_input={"cmd": "ls"},
        )
        assert decision.outcome == "approved"
        assert decision.metadata.get("source") == "rule_auto_allow"
        assert isinstance(decision, ApprovalDecision)
        assert manager.pending_count == 0
        assert manager.auto_approve_task_count == 0


# ---------------------------------------------------------------------------
# 用例 9：approval-rules-unified DoD 关键证据 — rm 危险规则强制人审
# ---------------------------------------------------------------------------


async def test_bash_rm_blocked_forces_human_approval_even_when_zap_on() -> None:
    """generic_chat + Bash(rm -rf ...) + Zap ON → 走真实 24 条规则 →
    命中 ``bash_rm_any`` → 强制人审，**不启 auto-approve**。

    approval-rules-unified DoD 关键证据：让 generic_chat 通道复用 claude_code
    24 规则保护——即使用户开了总开关，``rm`` 这类破坏性命令仍必须人审。

    构造真实 ``AutoApprovalPolicy + RuleSet (load_default_rules)`` +
    ``ConfigStore (cwd enabled=True)``，构造 manager.request → 启动短 timeout，
    确认：
    - 不创建 auto-approve task（``auto_approve_task_count == 0`` during pending）
    - timeout 触发后 outcome=rejected, source=manager_timeout（不是 rule_auto_allow）
    """
    from web.auto_approval.policy import AutoApprovalPolicy
    from web.auto_approval.rules import load_default_rules

    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ConfigStore(Path(tmp_dir))
        # Zap ON + 极短 timeout（让本测尽快收尾）
        store.set(ProjectConfig(cwd="/proj/danger", enabled=True, timeout_ms=200))
        rule_set = load_default_rules()  # 真实 24 规则
        policy = AutoApprovalPolicy(rule_set, store)

        manager = _make_manager_with_policy(policy=policy, default_timeout_ms=200)

        # 启动 request 后台 task，盯住 pending 期间 auto_approve_task_count
        request_task = asyncio.create_task(
            manager.request(
                channel="generic_chat",
                thread_id="t-rm-danger",
                cwd="/proj/danger",
                tool_name="Bash",
                tool_input={"command": "rm -rf /tmp/some-dir"},
            )
        )
        # 等 pending 注册
        for _ in range(50):
            if manager.pending_count > 0:
                break
            await asyncio.sleep(0.005)
        # 关键断言 1：未启动 auto-approve task（命中危险规则 → 强制人审）
        assert manager.auto_approve_task_count == 0, (
            "bash_rm_any matched → must NOT start auto-approve countdown even when cwd Zap is ON"
        )
        assert manager.pending_count == 1

        # 在 timeout 之前再断言 pending 一下里的 matched_rule（manager 内部状态）
        req_id = next(iter(manager._pending.keys()))
        pending = manager._pending[req_id]
        assert pending.matched_rule == "bash_rm_any", (
            "matched_rule 必须落到 pending dataclass 让 audit / UI 能展示拦截原因"
        )

        decision = await request_task
        # 关键断言 2：timeout 兜底拒绝（user 没人审），不是自动通过
        assert decision.outcome == "rejected"
        assert decision.metadata.get("source") == "manager_timeout"
        # R10 清干净
        assert manager.pending_count == 0
        assert manager.timeout_task_count == 0
        assert manager.auto_approve_task_count == 0
