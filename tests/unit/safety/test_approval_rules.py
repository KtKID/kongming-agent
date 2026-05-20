""":class:`safety.approval_rules.ApprovalRules` 单测。

覆盖 task #3（smart-approval-generic-chat-autoallow）新增的 ``config_reader``
注入路径 + classify 升级 + fail-closed 兜底。

测试覆盖矩阵（5 项）：

1. ``test_classify_default_returns_ask_60s_when_no_reader``
   —— 不传 config_reader（默认行为 0 回归），所有输入返回 ask + 60s。

2. ``test_classify_returns_auto_approve_at_ms_when_enabled``
   —— cwd config enabled=True 时返 ``_RuleDecision(auto_approve_at_ms=now+timeout,
   timeout_ms=cfg.timeout_ms)``。

3. ``test_classify_returns_default_when_disabled``
   —— cwd config enabled=False 时走默认（``auto_approve_at_ms is None``）。

4. ``test_classify_returns_default_when_cwd_unknown``
   —— cwd 不在 store 时（reader.get 返 None）走默认 ask + 60s。

5. ``test_classify_fail_closed_when_reader_raises``
   —— reader.get 抛异常时 **fail-closed** 走默认 ask + 60s，**异常不向上抛**
   （审批主流程不能因配置读取失败而中断）。

额外补充（非任务必修，加固覆盖）：

6. ``test_classify_non_generic_chat_ignores_reader``
   —— 其他通道（claude_code / cron / evolution / cli）即使 reader 配置 enabled=True
   也不查 reader，永远走默认。
7. ``test_classify_timeout_ms_zero_falls_back_to_default``
   —— ``ProjectConfig.timeout_ms == 0`` 时按 ``ConfigStore`` 字段约定降级到全局默认。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest

from safety.approval_rules import ApprovalRules, ProjectConfigLike, _RuleDecision

# ---------------------------------------------------------------------------
# 测试用 fake：duck typing 匹配 ProjectConfigLike + _ConfigReaderProto
# ---------------------------------------------------------------------------


@dataclass
class _FakeProjectConfig:
    """duck typing 匹配 :class:`safety.approval_rules.ProjectConfigLike`。

    与 ``web.auto_approval.config_store.ProjectConfig`` 同构（仅 enabled + timeout_ms
    两字段；rule_overrides 阶段 1.5 不参与判定，故省略）。
    """

    enabled: bool
    timeout_ms: int


class _FakeConfigReader:
    """duck typing 匹配 :class:`safety.approval_rules._ConfigReaderProto`。

    构造时传 dict[cwd, ProjectConfigLike]；``get`` 按 cwd 查表，未命中返 None。
    """

    def __init__(self, store: dict[str, ProjectConfigLike]) -> None:
        self._store = store

    def get(self, cwd: str) -> ProjectConfigLike | None:
        """按 cwd 查表；未命中返 None（模拟 ConfigStore.get 真实行为）。"""
        return self._store.get(cwd)


class _RaisingConfigReader:
    """模拟 reader 抛异常（用例 5 fail-closed 验证）。"""

    def get(self, cwd: str) -> ProjectConfigLike | None:
        """无条件抛 RuntimeError 模拟 IO / 反序列化失败。"""
        raise RuntimeError(f"simulated IO error for cwd={cwd}")


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def common_input() -> dict[str, Any]:
    """classify 调用的通用参数（thread_id / tool_name / tool_input 阶段 1.5 不参与判定）。"""
    return {
        "thread_id": "thread-test-001",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }


# ---------------------------------------------------------------------------
# 用例 1：默认行为 0 回归
# ---------------------------------------------------------------------------


def test_classify_default_returns_ask_60s_when_no_reader(common_input: dict[str, Any]) -> None:
    """不传 config_reader 时，classify 对任何 channel 都返回默认 ask + 60s。

    保证 task #3 改动对阶段 1 既有行为 **0 回归**。
    """
    rules = ApprovalRules()  # config_reader=None
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
# 用例 2：cwd enabled=True → 返 auto_approve_at_ms
# ---------------------------------------------------------------------------


def test_classify_returns_auto_approve_at_ms_when_enabled(
    common_input: dict[str, Any],
) -> None:
    """generic_chat 通道 + cwd config.enabled=True 时返 auto_approve 倒计时。

    断言：
    - ``auto_approve_at_ms`` ≈ now + cfg.timeout_ms（±100ms 容差）
    - ``timeout_ms`` == cfg.timeout_ms
    - 其他字段保持默认结构（is_immediate=False / severity=standard / matched_rule=None）
    """
    cfg = _FakeProjectConfig(enabled=True, timeout_ms=15_000)
    reader = _FakeConfigReader({"/proj/foo": cfg})
    rules = ApprovalRules(default_timeout_ms=60_000, config_reader=reader)

    before_ms = int(time.time() * 1000)
    dec = rules.classify(channel="generic_chat", cwd="/proj/foo", **common_input)
    after_ms = int(time.time() * 1000)

    assert dec.is_immediate is False
    assert dec.immediate_outcome is None
    assert dec.matched_rule is None
    assert dec.severity == "standard"
    assert dec.auto_reject_at_ms is None
    assert dec.timeout_ms == 15_000
    assert dec.auto_approve_at_ms is not None
    # 容差：now + 15000 ± (after_ms - before_ms + 1ms)
    assert before_ms + 15_000 <= dec.auto_approve_at_ms <= after_ms + 15_000 + 1


# ---------------------------------------------------------------------------
# 用例 3：cwd enabled=False → 走默认
# ---------------------------------------------------------------------------


def test_classify_returns_default_when_disabled(common_input: dict[str, Any]) -> None:
    """cwd 已配置但 enabled=False 时走默认 ask + 60s（auto_approve_at_ms is None）。

    enabled=False 表示「用户主动关掉了此 cwd 的自动通过」，按默认 ask 处理。
    """
    cfg = _FakeProjectConfig(enabled=False, timeout_ms=30_000)
    reader = _FakeConfigReader({"/proj/foo": cfg})
    rules = ApprovalRules(default_timeout_ms=60_000, config_reader=reader)

    dec = rules.classify(channel="generic_chat", cwd="/proj/foo", **common_input)

    assert dec.auto_approve_at_ms is None
    assert dec.auto_reject_at_ms is None
    assert dec.timeout_ms == 60_000
    assert dec.severity == "standard"


# ---------------------------------------------------------------------------
# 用例 4：cwd 不在 store → 走默认
# ---------------------------------------------------------------------------


def test_classify_returns_default_when_cwd_unknown(common_input: dict[str, Any]) -> None:
    """cwd 不在 ConfigStore（reader.get 返 None）时走默认 ask + 60s。

    新 cwd 默认 = ask，与「用户从未配置过」行为一致。
    """
    reader = _FakeConfigReader({})  # 空 store，任何 cwd 都返 None
    rules = ApprovalRules(default_timeout_ms=60_000, config_reader=reader)

    dec = rules.classify(channel="generic_chat", cwd="/proj/new", **common_input)

    assert dec.auto_approve_at_ms is None
    assert dec.timeout_ms == 60_000


# ---------------------------------------------------------------------------
# 用例 5：reader 抛异常 → fail-closed 默认 ask
# ---------------------------------------------------------------------------


def test_classify_fail_closed_when_reader_raises(common_input: dict[str, Any]) -> None:
    """reader.get 抛异常时 fail-closed 走默认 ask + 60s，**异常不向上抛**。

    审批主流程不能因配置读取失败而中断（与 manager 用 ``gather(...,
    return_exceptions=True)`` 包裹 sink 调用的设计思路一致）。
    """
    reader = _RaisingConfigReader()
    rules = ApprovalRules(default_timeout_ms=60_000, config_reader=reader)

    # 关键断言：调用本身不抛异常
    dec = rules.classify(channel="generic_chat", cwd="/proj/foo", **common_input)

    assert dec.auto_approve_at_ms is None
    assert dec.auto_reject_at_ms is None
    assert dec.timeout_ms == 60_000
    assert dec.severity == "standard"
    assert isinstance(dec, _RuleDecision)


# ---------------------------------------------------------------------------
# 用例 6（加固）：非 generic_chat 通道忽略 reader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["claude_code", "cron", "evolution", "cli"])
def test_classify_non_generic_chat_ignores_reader(
    channel: str,
    common_input: dict[str, Any],
) -> None:
    """非 generic_chat 通道即使 reader 配置 enabled=True 也走默认 ask（不查 reader）。

    阶段 1.5 仅 generic_chat 启用 per-cwd 自动通过；其他通道接入由阶段 2-3 task 各自处理。
    """
    cfg = _FakeProjectConfig(enabled=True, timeout_ms=10_000)
    reader = _FakeConfigReader({"/proj/foo": cfg})
    rules = ApprovalRules(default_timeout_ms=60_000, config_reader=reader)

    dec = rules.classify(channel=channel, cwd="/proj/foo", **common_input)

    assert dec.auto_approve_at_ms is None, f"channel={channel} should not auto-approve"
    assert dec.timeout_ms == 60_000


# ---------------------------------------------------------------------------
# 用例 7（加固）：timeout_ms == 0 降级到全局默认
# ---------------------------------------------------------------------------


def test_classify_timeout_ms_zero_falls_back_to_default(
    common_input: dict[str, Any],
) -> None:
    """cfg.timeout_ms == 0 表示「用全局默认」，降级到 default_timeout_ms。

    与 :class:`web.auto_approval.config_store.ProjectConfig` 的字段约定一致
    （0 = sentinel 标记，让全局配置生效）。
    """
    cfg = _FakeProjectConfig(enabled=True, timeout_ms=0)  # sentinel: 用全局
    reader = _FakeConfigReader({"/proj/foo": cfg})
    rules = ApprovalRules(default_timeout_ms=45_000, config_reader=reader)

    before_ms = int(time.time() * 1000)
    dec = rules.classify(channel="generic_chat", cwd="/proj/foo", **common_input)
    after_ms = int(time.time() * 1000)

    assert dec.auto_approve_at_ms is not None
    assert dec.timeout_ms == 45_000  # 降级到全局默认
    assert before_ms + 45_000 <= dec.auto_approve_at_ms <= after_ms + 45_000 + 1
