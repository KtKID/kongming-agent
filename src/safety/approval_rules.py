"""审批策略层容器（阶段 1 空骨架 + 阶段 1.5 generic_chat per-cwd 自动通过注入）。

manager 调 `classify(channel, thread_id, cwd, tool_name, tool_input)` 拿初步决策。

阶段 1：所有输入都返回 `_RuleDecision(is_immediate=False, severity='standard', timeout_ms=60_000)`，
意思 = "需要用户决策，60s 默认 timeout"。

阶段 1.5（task #3）：generic_chat 通道可注入 :class:`_ConfigReaderProto`
（web 装配点把 ``web.auto_approval.config_store.ConfigStore`` 包成本接口注入），
启用 per-cwd 自动通过倒计时——cwd config.enabled=True 时把 ``auto_approve_at_ms``
透传给 manager，manager 在倒计时到点把 pending future set_result(approved)。
对其他通道（claude_code / cron / evolution / cli）保持阶段 1 默认行为不变。

阶段 2-3：迁通道时把规则注入（claude_code 24 条 / cron auto-allow / evolution auto-allow），
但仍 hardcode 在 Python 里。

阶段 5：从 rules.yaml 加载完整 schema（per-channel + per-thread + per-cwd 三层 scope）。

设计真源：docs/safety-approval-manager-v0.5/30-rules-schema.md
        docs/safety-approval-manager-v0.5/（task #3 spec）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# duck-typing Protocols（避免 safety → web 跨层 import，import-linter Contract 强制）
# ---------------------------------------------------------------------------


class ProjectConfigLike(Protocol):
    """对 :class:`web.auto_approval.config_store.ProjectConfig` 的 duck typing 协议。

    仅读 ``enabled`` + ``timeout_ms`` 两字段；阶段 1.5 的 generic_chat 自动通过
    路径忽略 ``rule_overrides``（仅依赖 cwd 维度的总开关 + 超时）。

    用 class body 字段形式声明（不是 ``@property``），与 dataclass 字段
    结构匹配；与 :class:`safety.inbox_event_sink._BroadcasterProto` 同款解耦。
    """

    enabled: bool
    timeout_ms: int


class _ConfigReaderProto(Protocol):
    """per-cwd 配置读取协议（duck typing 注入；避免 safety→web 跨层 import）。

    web 装配时把 :class:`web.auto_approval.config_store.ConfigStore` 直接传进
    :class:`ApprovalRules`，结构匹配本 Protocol 即可（无需显式继承）。
    与 stage1 的 :class:`safety.inbox_event_sink._BroadcasterProto` 同款解耦模式。
    """

    def get(self, cwd: str) -> ProjectConfigLike | None:
        """按 cwd 取 per-project 配置；未配置返回 None。"""
        ...


class ApprovalRules:
    """per-channel + per-thread + per-cwd 三层 scope 的规则容器（阶段 1 空骨架）。

    阶段 1：仅持空骨架，所有输入返回 "ask + standard + 60s timeout" 默认决策。
    阶段 1.5（task #3）：可选注入 :class:`_ConfigReaderProto` 启用 generic_chat
    通道的 per-cwd 自动通过倒计时（cwd config.enabled=True → auto_approve_at_ms
    透传到 manager）。
    阶段 2-3：claude_code 24 规则 / cron auto-allow / evolution auto-allow 迁入（hardcode）。
    阶段 5：完整 yaml schema 加载。
    """

    def __init__(
        self,
        *,
        default_timeout_ms: int = 60_000,
        config_reader: _ConfigReaderProto | None = None,
    ) -> None:
        """构造空骨架 ApprovalRules（可选注入 ``config_reader`` 启用 per-cwd 自动通过）。

        Args:
            default_timeout_ms: 未匹配规则时的默认 timeout（毫秒），默认 60s
            config_reader: 阶段 1.5 generic_chat per-cwd 自动通过配置读取器；
                None = 关闭自动通过（保持阶段 1 默认 ask 行为）。装配点典型注入：
                ``web.auto_approval.config_store.ConfigStore`` 实例
                （duck typing 匹配 :class:`_ConfigReaderProto`）。
        """
        self._default_timeout_ms = default_timeout_ms
        self._config_reader = config_reader

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
        阶段 1.5（task #3）：若注入 ``config_reader`` 且 channel=='generic_chat' 且
        cwd config.enabled=True，返 ``_RuleDecision(auto_approve_at_ms=now+timeout)``
        让 manager 启动自动通过倒计时；其他通道仍走默认 ask。
        阶段 2-3 改：按 channel 分支匹配规则。
        阶段 5 改：从 yaml 三层 scope 命中。

        **fail-closed 兜底**：``config_reader.get`` 抛任何异常时降级走默认 ask
        + 60s，不向上抛——审批主流程不能因配置读取失败而中断
        （与 manager 用 ``asyncio.gather(..., return_exceptions=True)`` 包裹
        sink 调用的设计思路一致）。

        Args:
            channel: 'claude_code' / 'generic_chat' / 'cron' / 'evolution' / 'cli'
            thread_id: 通道内 thread 标识
            cwd: 触发审批的工作目录
            tool_name: 工具名（如 "Bash" / "Edit"）
            tool_input: 工具参数 dict

        Returns:
            _RuleDecision；阶段 1.5 generic_chat enabled cwd 返自动通过倒计时，
            其他情况恒为 "ask + standard + 60s"
        """
        # thread_id / tool_name / tool_input 阶段 1.5 不参与判定（阶段 5 才用）
        _ = (thread_id, tool_name, tool_input)

        # 阶段 1.5：仅 generic_chat 通道才查 reader（其他通道走默认 ask）
        if channel == "generic_chat" and self._config_reader is not None:
            try:
                cfg = self._config_reader.get(cwd)
            except Exception:
                # fail-closed：reader 异常不影响审批主流程，走默认 ask
                logger.exception(
                    "config_reader.get(cwd=%r) raised; falling back to default ask",
                    cwd,
                )
                cfg = None
            if cfg is not None and cfg.enabled:
                # ProjectConfig.timeout_ms == 0 表示「用全局默认」（见 web.auto_approval
                # .config_store.ProjectConfig 字段约定），这里降级到 default_timeout_ms
                timeout = cfg.timeout_ms if cfg.timeout_ms else self._default_timeout_ms
                now_ms = int(time.time() * 1000)
                return _RuleDecision(
                    is_immediate=False,
                    immediate_outcome=None,
                    matched_rule=None,
                    severity="standard",
                    auto_approve_at_ms=now_ms + timeout,
                    auto_reject_at_ms=None,
                    timeout_ms=timeout,
                )

        # 默认：所有其他输入返 ask + 60s
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
