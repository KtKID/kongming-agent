"""generic_chat per-cwd 自动通过倒计时端到端集成测（task #6）。

覆盖 :class:`safety.approval_manager.ApprovalManager._handle_auto_approve` 的
完整生命周期 + 三退出路径 cancel 防护 + race fail-safe：

1. ``test_auto_approve_resolves_when_cwd_enabled`` ——
   ConfigReader 返 enabled=True + 短 timeout → manager.request 阻塞 → 等
   delay → future 自动 set_result(approved + source='rule_auto_allow')。

2. ``test_auto_approve_does_not_start_when_cwd_disabled`` ——
   ConfigReader 返 enabled=False → 不创建 auto_approve_task；
   ``manager.auto_approve_task_count == 0`` 期间断言；
   保持原 ask + 60s 行为，必须等 timeout / cancel 才返回。

3. ``test_user_resolve_cancels_auto_approve_task`` ——
   启 auto-approve timer（delay 充足）→ 用户在 timer 触发前 ``manager.resolve``
   → request() finally 走 ``_cleanup_pending`` → 同步 cancel auto-approve task
   → 断言 task.cancelled() == True 且 decision.source == 'user'（不是 'rule_auto_allow'）。

4. ``test_manager_cancel_cancels_auto_approve_task`` ——
   启 auto-approve timer → ``manager.cancel`` → cleanup cancel auto-approve task。

5. ``test_timeout_cancels_auto_approve_task`` ——
   timeout_ms 小于 auto_approve delay 不太现实（auto_approve_at_ms 来自 timeout_ms
   本身），所以本测改造为 cancel_by_thread 走 cleanup 路径，确保 task 被清理。

6. ``test_race_safe_when_user_resolve_and_auto_approve_fire_simultaneously`` ——
   极短 auto-approve delay 与 user resolve 同时触发的 race：
   future 只被 set_result 一次（由 future.done() 守卫），不抛 ``InvalidStateError``。

7. ``test_config_store_shared_across_channels`` ——
   关键证据：装配时 ``ApprovalRules._config_reader is policy.config_store``
   是同一份对象引用；UI toggle 写盘后 ``ApprovalRules.classify`` 立即读到新值
   （不缓存）。这是任务 #6 "用户视角'开关一处管所有通道'" 的架构保证。

模拟 ws / 不实际启 FastAPI TestClient：复用 :mod:`test_generic_chat_inbox_e2e`
的 ``_make_integrated_setup`` 思路，集中验证 manager + ApprovalRules + ConfigStore
三件套的端到端集成。
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.contracts import ApprovalDecision
from safety.approval_manager import (
    ApprovalManager,
    reset_for_testing,
)
from safety.approval_rules import ApprovalRules, ProjectConfigLike
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
class _FakeProjectConfig:
    """duck typing 匹配 :class:`safety.approval_rules.ProjectConfigLike`。"""

    enabled: bool
    timeout_ms: int


class _FakeConfigReader:
    """内存 ConfigReader，按 cwd → ProjectConfigLike 查表。"""

    def __init__(self, store: dict[str, ProjectConfigLike]) -> None:
        self._store = store

    def get(self, cwd: str) -> ProjectConfigLike | None:
        return self._store.get(cwd)


def _make_manager_with_reader(
    *,
    reader: _FakeConfigReader | None,
    default_timeout_ms: int = 10_000,
) -> ApprovalManager:
    """构造裸 manager 实例（注入 ApprovalRules + 可选 reader）。"""
    return ApprovalManager(
        rules=ApprovalRules(
            default_timeout_ms=default_timeout_ms,
            config_reader=reader,
        ),
        default_timeout_ms=default_timeout_ms,
    )


# ---------------------------------------------------------------------------
# 用例 1：cwd enabled=True → 自动 resolve
# ---------------------------------------------------------------------------


async def test_auto_approve_resolves_when_cwd_enabled() -> None:
    """generic_chat + cwd enabled=True + 短 timeout → request 自动 set_result(approved)。

    断言：
    - decision.outcome == 'approved'
    - decision.metadata.source == 'rule_auto_allow'（区分 user 决策）
    - R10：pending / timeout_tasks / auto_approve_tasks 全清空
    """
    # cfg.timeout_ms=100ms 决定 auto_approve delay；manager.request 传更大的
    # timeout_ms=5_000 覆盖 timeout task delay，确保 auto-approve **先于** timeout 触发
    # （否则两者同 delay 时谁先无保证；spec 设计 auto-approve 应该早一步）
    reader = _FakeConfigReader({"/proj/foo": _FakeProjectConfig(enabled=True, timeout_ms=100)})
    manager = _make_manager_with_reader(reader=reader, default_timeout_ms=10_000)

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
    # R10 全清
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0
    assert manager.auto_approve_task_count == 0


# ---------------------------------------------------------------------------
# 用例 2：cwd enabled=False → 不启动 auto-approve task
# ---------------------------------------------------------------------------


async def test_auto_approve_does_not_start_when_cwd_disabled() -> None:
    """cwd config.enabled=False → ApprovalRules 不返 auto_approve_at_ms →
    manager 不创建 auto-approve task；request 仍走原 ask 路径（必须等 timeout
    / cancel 才返回）。

    用极短 default_timeout_ms 触发 fail-closed timeout，断言：
    - decision.source == 'manager_timeout'（不是 'rule_auto_allow'）
    - 中途 ``auto_approve_task_count == 0``
    """
    reader = _FakeConfigReader({"/proj/foo": _FakeProjectConfig(enabled=False, timeout_ms=100)})
    manager = _make_manager_with_reader(reader=reader, default_timeout_ms=100)

    request_task = asyncio.create_task(
        manager.request(
            channel="generic_chat",
            thread_id="t-disabled",
            cwd="/proj/foo",
            tool_name="Bash",
            tool_input={"cmd": "ls"},
        )
    )
    # 让 request 跑到注册 pending 那一步
    for _ in range(50):
        if manager.pending_count > 0:
            break
        await asyncio.sleep(0.005)
    # 关键断言：未启动 auto-approve task
    assert manager.auto_approve_task_count == 0
    # 等 timeout 自然触发
    decision = await request_task
    assert decision.outcome == "rejected"
    assert decision.metadata.get("source") == "manager_timeout"
    # R10 全清
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0
    assert manager.auto_approve_task_count == 0


# ---------------------------------------------------------------------------
# 用例 3：user resolve 提前触发 → cleanup cancel auto-approve task
# ---------------------------------------------------------------------------


async def test_user_resolve_cancels_auto_approve_task() -> None:
    """启 auto-approve timer (delay=10s) → 用户在 timer 触发前 resolve → cleanup
    同步 cancel auto-approve task。

    断言：
    - decision.source == 'user'（不是 'rule_auto_allow'，证明 user 路径优先）
    - request 返回后 auto_approve_task_count == 0
    - 没有 InvalidStateError 抛出（future 只被 set 一次）
    """
    # auto_approve delay = 10s（足够长，绝不会先触发）
    reader = _FakeConfigReader({"/proj/foo": _FakeProjectConfig(enabled=True, timeout_ms=10_000)})
    manager = _make_manager_with_reader(reader=reader, default_timeout_ms=60_000)

    async def user_resolve_quickly() -> None:
        # 等 pending 注册好（注意 auto_approve_task 在同一 lock 内一起注册）
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

    # user 路径优先 → source = 'user'
    assert decision.outcome == "approved"
    assert decision.metadata.get("source") == "user"
    # R10 全清
    assert manager.pending_count == 0
    assert manager.timeout_task_count == 0
    assert manager.auto_approve_task_count == 0


# ---------------------------------------------------------------------------
# 用例 4：manager.cancel 触发 → cleanup cancel auto-approve task
# ---------------------------------------------------------------------------


async def test_manager_cancel_cancels_auto_approve_task() -> None:
    """启 auto-approve timer → manager.cancel → cleanup cancel auto-approve task。

    断言：
    - decision.source == 'manager_cancel'（不是 'rule_auto_allow'）
    - auto_approve_task_count == 0 after cleanup
    """
    reader = _FakeConfigReader({"/proj/foo": _FakeProjectConfig(enabled=True, timeout_ms=10_000)})
    manager = _make_manager_with_reader(reader=reader, default_timeout_ms=60_000)

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

    覆盖 WebHostAdapter.close 行为对 auto-approve task 的连带清理。
    """
    reader = _FakeConfigReader({"/proj/foo": _FakeProjectConfig(enabled=True, timeout_ms=10_000)})
    manager = _make_manager_with_reader(reader=reader, default_timeout_ms=60_000)

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

    本测断言：多轮跑（10 次）下，request 都能正常返回，decision.outcome 必然
    是 'approved'（两侧路径都 approve），且 pending / tasks 都清干净。
    """
    for _ in range(10):
        reader = _FakeConfigReader({"/proj/foo": _FakeProjectConfig(enabled=True, timeout_ms=10)})
        manager = _make_manager_with_reader(reader=reader, default_timeout_ms=60_000)

        async def aggressive_user_resolve(mgr: ApprovalManager) -> None:
            # 不等 pending 注册，尽快 resolve（与 auto-approve 抢窗口）
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

        # 不管谁先：outcome 必然是 approved，没异常
        assert decision.outcome == "approved"
        assert manager.pending_count == 0
        assert manager.timeout_task_count == 0
        assert manager.auto_approve_task_count == 0


# ---------------------------------------------------------------------------
# 用例 7：架构验证 — 同一份 ConfigStore 跨 channel 共享
# ---------------------------------------------------------------------------


async def test_config_store_shared_across_channels() -> None:
    """关键证据：generic_chat 的 ApprovalRules 与 claude_code 路径用 **同一份**
    ConfigStore 实例 → UI 一处 toggle 即时生效于所有通道。

    构造真实 ConfigStore + AutoApprovalPolicy（claude_code 路径用），断言：

    1. ``policy.config_store is store`` —— 公开 property 暴露同一份对象
    2. ``ApprovalRules(config_reader=policy.config_store)`` 注入后，
       ConfigStore.set(...) 写盘 → ApprovalRules.classify(...) 立即读到新值
       （ConfigStore 不缓存，每次 ``get`` 读盘；保证多通道一致性）。
    """
    from web.auto_approval.policy import AutoApprovalPolicy
    from web.auto_approval.rules import RuleSet

    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ConfigStore(Path(tmp_dir))
        rule_set = RuleSet(version=1, default_timeout_ms=10_000, rules=())
        policy = AutoApprovalPolicy(rule_set, store)

        # 证据 1：property 暴露同一份对象
        assert policy.config_store is store, (
            "policy.config_store must be the same ConfigStore instance "
            "(architectural contract: single source of truth for cwd config)"
        )

        # 证据 2：注入后跨通道生效
        rules = ApprovalRules(default_timeout_ms=60_000, config_reader=policy.config_store)

        # 初始：cwd 未配置 → classify 默认 ask
        dec1 = rules.classify(
            channel="generic_chat",
            thread_id="t",
            cwd="/proj/shared",
            tool_name="Bash",
            tool_input={},
        )
        assert dec1.auto_approve_at_ms is None

        # UI 等价操作：claude_code 路径通过 policy.set_enabled 写盘
        policy.set_enabled("/proj/shared", True)

        # 立即查：generic_chat 路径已经读到新配置（不缓存）
        dec2 = rules.classify(
            channel="generic_chat",
            thread_id="t",
            cwd="/proj/shared",
            tool_name="Bash",
            tool_input={},
        )
        assert dec2.auto_approve_at_ms is not None, (
            "shared ConfigStore must propagate UI toggle to generic_chat channel"
        )

        # 反向：toggle off 也立即生效
        policy.set_enabled("/proj/shared", False)
        dec3 = rules.classify(
            channel="generic_chat",
            thread_id="t",
            cwd="/proj/shared",
            tool_name="Bash",
            tool_input={},
        )
        assert dec3.auto_approve_at_ms is None


# ---------------------------------------------------------------------------
# 用例 8：真实 ConfigStore 端到端 → manager auto-resolve
# ---------------------------------------------------------------------------


async def test_real_config_store_drives_manager_auto_approve() -> None:
    """端到端 happy path：真实 ConfigStore 写一条 enabled=True + 短 timeout →
    manager.request 自动 resolve approved。

    与用例 1 的差异：用真实 ConfigStore 而非 _FakeConfigReader，验证
    duck typing 在生产对象上确实匹配 _ConfigReaderProto（避免 Protocol 漂移）。
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ConfigStore(Path(tmp_dir))
        # 写一条 cwd 配置：enabled + 200ms timeout
        store.set(ProjectConfig(cwd="/proj/real", enabled=True, timeout_ms=200))

        manager = _make_manager_with_reader(reader=store, default_timeout_ms=10_000)  # type: ignore[arg-type]

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
