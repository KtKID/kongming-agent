"""审批策略层容器（阶段 1 空骨架）。

manager 调 `classify(channel, thread_id, cwd, tool_name, tool_input)` 拿初步决策。
阶段 1：所有输入都返回 `_RuleDecision(is_immediate=False, severity='standard', timeout_ms=60_000)`，
意思 = "需要用户决策，60s 默认 timeout"。

阶段 2-3：迁通道时把规则注入（claude_code 24 条 / cron auto-allow / evolution auto-allow），
但仍 hardcode 在 Python 里。

阶段 5：从 rules.yaml 加载完整 schema（per-channel + per-thread + per-cwd 三层 scope）。

设计真源：docs/safety-approval-manager-v0.5/30-rules-schema.md
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class _RuleDecision:
    """rules.classify 的返回类型。

    Attributes:
        is_immediate: True = 跳过用户直接决策（auto_allow / auto_reject）；
            False = 需要 explicit_consent（manager 创建 pending future）
        immediate_outcome: is_immediate=True 时的 outcome（'allowed' / 'rejected'）；
            is_immediate=False 时无意义
        matched_rule: 命中的规则 ID（如 "Bash(rm:*)"）；未命中 = None
        severity: 'standard' / 'elevated'（用于 inbox payload + UI 渲染）
        auto_approve_at_ms: 安全路径倒计时到点 ms；阶段 1 generic_chat 不用，None
        auto_reject_at_ms: 危险路径倒计时到点 ms；阶段 1 generic_chat 不用，None
        timeout_ms: 用户决策的超时阈值；阶段 1 默认 60_000（与 WebHostAdapter._timeout 一致）
    """

    is_immediate: bool
    immediate_outcome: str | None
    matched_rule: str | None
    severity: str
    auto_approve_at_ms: int | None
    auto_reject_at_ms: int | None
    timeout_ms: int | None


class ApprovalRules:
    """per-channel + per-thread + per-cwd 三层 scope 的规则容器（阶段 1 空骨架）。

    阶段 1：仅持空骨架，所有输入返回 "ask + standard + 60s timeout" 默认决策。
    阶段 2-3：claude_code 24 规则 / cron auto-allow / evolution auto-allow 迁入（hardcode）。
    阶段 5：完整 yaml schema 加载。
    """

    def __init__(self, *, default_timeout_ms: int = 60_000) -> None:
        """构造空骨架 ApprovalRules。

        Args:
            default_timeout_ms: 未匹配规则时的默认 timeout（毫秒），默认 60s
        """
        self._default_timeout_ms = default_timeout_ms

    def classify(
        self,
        *,
        channel: str,
        thread_id: str,
        cwd: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> _RuleDecision:
        """根据 channel + thread + cwd + tool 拿初步决策。

        阶段 1 实现：所有输入都返回 "ask + standard + 60s"。
        阶段 2-3 改：按 channel 分支匹配规则。
        阶段 5 改：从 yaml 三层 scope 命中。

        Args:
            channel: 'claude_code' / 'generic_chat' / 'cron' / 'evolution' / 'cli'
            thread_id: 通道内 thread 标识
            cwd: 触发审批的工作目录
            tool_name: 工具名（如 "Bash" / "Edit"）
            tool_input: 工具参数 dict

        Returns:
            _RuleDecision；阶段 1 恒为 "ask + standard + 60s"
        """
        # 阶段 1：忽略所有输入，恒返回 default
        _ = (channel, thread_id, cwd, tool_name, tool_input)
        return _RuleDecision(
            is_immediate=False,
            immediate_outcome=None,
            matched_rule=None,
            severity="standard",
            auto_approve_at_ms=None,
            auto_reject_at_ms=None,
            timeout_ms=self._default_timeout_ms,
        )

    def add_session_grant(
        self,
        *,
        channel: str,
        thread_id: str,
        cwd: str,
        tool_name: str,
    ) -> None:
        """用户选了"本 session 都同意"后调用，向 thread 级 overrides 加规则。

        阶段 1：no-op（generic_chat 卡片已隐藏「本 session」按钮，不会调到这）。
        阶段 5：写 _thread_overrides，thread 销毁时清空。
        """
        _ = (channel, thread_id, cwd, tool_name)
        # 阶段 1 no-op
