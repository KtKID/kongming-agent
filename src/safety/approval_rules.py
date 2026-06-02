"""审批策略层容器（approval-rules-unified：generic_chat 复用统一 AutoApprovalPolicy）。

manager 调 ``classify(channel, thread_id, cwd, tool_name, tool_input)`` 拿初步决策。

设计目标：**一份 rule，多通道共用**——让 generic_chat 通道完全复用 claude_code
已有的成熟 :class:`web.auto_approval.policy.AutoApprovalPolicy` + ``default_rules.yaml``
+ per-cwd :class:`web.auto_approval.config_store.ConfigStore`，**不再两套并存**。

设计真源：
- ``dev-pipeline/tasks/approval-rules-unified/README.md`` — 技术设计 + DoD
- ``docs/safety-approval-manager-v0.5/30-rules-schema.md`` — 长期 schema 演进

duck typing 边界（safety 不直接 import web）：
通过 :class:`_AutoApprovalPolicyProto` + :class:`_PolicyDecisionLike` Protocol
注入 :class:`AutoApprovalPolicy` 实例，import-linter Contract 强制
``safety → web`` 跨层 import 不存在；装配点在 ``src/web/run.py``。

claude_code 通道仍走 :meth:`web.host_adapter.WebHostAdapter.prompt_approval`
直调 policy，**不经** ApprovalRules；本模块仅服务 generic_chat 路径。
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
        matched_rule: 命中的规则 ID（如 ``"bash_rm_any"``）；未命中 = None
        severity: ``'standard'`` / ``'elevated'``（用于 inbox payload + UI 渲染）
        auto_approve_at_ms: 安全路径倒计时到点 ms；非自动通过路径恒 None
        auto_reject_at_ms: 危险路径倒计时到点 ms；当前实现恒 None（规则命中改强制人审，不倒计时拒绝）
        timeout_ms: 用户决策的超时阈值；fallback 60_000（与 WebHostAdapter._timeout 默认一致）
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


class _PolicyDecisionLike(Protocol):
    """对 :class:`web.auto_approval.policy.Decision` 的 duck typing 协议。

    仅消费 ``auto_eligible`` / ``blocked_by_rule`` / ``timeout_ms`` 三字段
    （``rule_evaluation`` audit 快照本层不消费）。

    实际真源签名：``src/web/auto_approval/policy.py:33-46`` —— ``Decision`` 是
    ``@dataclass(frozen=True, slots=True)``，字段以 attribute 暴露，duck-typing
    匹配本 Protocol 即可（无需显式继承）。
    """

    @property
    def auto_eligible(self) -> bool: ...

    @property
    def blocked_by_rule(self) -> str | None: ...

    @property
    def timeout_ms(self) -> int: ...


class _AutoApprovalPolicyProto(Protocol):
    """对 :class:`web.auto_approval.policy.AutoApprovalPolicy` 的 duck typing 协议。

    仅消费 ``classify`` + ``is_enabled_for`` 两方法（``set_enabled`` / ``get_config``
    等 UI 写入路径不在 safety 层使用）。

    实际真源签名：``src/web/auto_approval/policy.py:107-114``::

        def classify(
            self,
            *,
            tool_name: str,
            tool_input: dict[str, Any],
            cwd: str,
            is_elevated: bool,
        ) -> Decision: ...

    与 :class:`safety.inbox_event_sink._BroadcasterProto` 同款解耦模式
    （stage1 任务沿用至今）。
    """

    def classify(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: str,
        is_elevated: bool,
    ) -> _PolicyDecisionLike: ...

    def is_enabled_for(self, cwd: str) -> bool: ...


# ---------------------------------------------------------------------------
# ApprovalRules
# ---------------------------------------------------------------------------


class ApprovalRules:
    """统一规则代理层——把 generic_chat 通道的 ``classify`` 委托给注入的
    :class:`AutoApprovalPolicy` 实例。

    架构：完整复用 policy 的 ``classify``（含 24 条规则匹配 + cwd 开关 +
    audit 快照），不再仅查 cwd 总开关 + timeout。

    历史变更（approval-rules-unified）：见 changelog；旧 Protocol / 字段架构
    已废弃，不在 source 文档保留旧名字（避免外部脆弱 grep 误匹配）。

    fail-closed 设计：
    ``policy=None`` 时（test 环境 / lifespan 漂移）走默认 ask + 60s，
    保证审批主流程在配置缺失时仍可阻塞等用户决策（**不开自动通过倒计时**）。

    阶段范围：
    - 当前：仅 generic_chat 通道；其他通道（cron / evolution / cli / claude_code）
      恒走默认 ask + 60s（claude_code 自走 WebHostAdapter.prompt_approval）
    - 阶段 5（spec 演进）：rules.yaml 完整 schema + per-thread + session grants
    """

    def __init__(
        self,
        *,
        policy: _AutoApprovalPolicyProto | None = None,
    ) -> None:
        """构造 ApprovalRules（注入可选 AutoApprovalPolicy）。

        Args:
            policy: 智能审批决策器实例（duck-typed 真源
                :class:`web.auto_approval.policy.AutoApprovalPolicy`）；
                ``None`` = fail-closed 走默认 ask（test 环境 / app.state 未就绪时
                的安全网）。
        """
        self._policy = policy
        # generic-chat-session-grant：thread 级 session 同意 overrides；
        # key = thread_id；value = set of (cwd, tool_name) 元组。
        # - 用户点弹卡「本 session 都同意」→ ApprovalManager.resolve 调
        #   add_session_grant 写入；
        # - 下次同 thread + cwd + tool 的 classify 命中此集 → immediate allow
        #   （但仍要查 policy.blocked_by_rule，危险规则优先级最高）；
        # - thread 销毁（cancel_by_thread）时调 clear_thread_grants 清空，
        #   避免内存泄漏（R10 同款）；
        # - **不持久化**：重启进程即清空（spec 阶段 5 才上 rules.yaml 持久化）。
        self._thread_overrides: dict[str, set[tuple[str, str]]] = {}

    def classify(
        self,
        *,
        channel: str,
        thread_id: str,
        cwd: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> _RuleDecision:
        """根据 channel + cwd + tool 拿初步决策。

        语义（与 :meth:`AutoApprovalPolicy.classify` 对齐）::

            if policy is None:                     → fail-closed 默认 ask + 60s
            elif channel != 'generic_chat':        → 默认 ask + 60s（其他通道不接入）
            elif pdec.blocked_by_rule:             → 强制人审（不启 auto-approve；matched_rule 透传）
            elif pdec.auto_eligible and enabled:   → auto-approve 倒计时（timeout fallback 60s）
            else:                                  → 默认 ask + 60s

        ``is_elevated`` 阶段 1 generic_chat 都按 ``False``（spec 阶段 5 才区分；
        codex / cli 后续接入时再处理）。

        **fail-closed 兜底**：``policy.classify`` 抛任何异常时降级走默认 ask + 60s，
        不向上抛——审批主流程不能因配置读取失败而中断（与 manager 用
        ``asyncio.gather(..., return_exceptions=True)`` 包裹 sink 调用同款思路）。

        Args:
            channel: ``'claude_code'`` / ``'generic_chat'`` / ``'cron'`` / ``'evolution'`` / ``'cli'``
            thread_id: 通道内 thread 标识（阶段 1 不参与判定）
            cwd: 触发审批的工作目录
            tool_name: 工具名（如 ``"Bash"`` / ``"Edit"``）
            tool_input: 工具参数 dict

        Returns:
            ``_RuleDecision``；severity 恒 ``"standard"``（阶段 5 才区分 elevated）。
        """
        # 0. thread 级 session grant（generic-chat-session-grant）：
        #    用户在此 thread 之前点过「本 session 都同意」→ 命中后**仍要查
        #    policy 的 blocked_by_rule**，危险规则（rm 等）优先级最高，绝不允许
        #    session grant 绕过守护；policy 允许 / policy 缺失 → immediate allow。
        if channel == "generic_chat" and (cwd, tool_name) in self._thread_overrides.get(
            thread_id, set()
        ):
            # 仍要查 policy 是否命中 blocked_by_rule（防止 session grant 绕过 rm 守护）
            if self._policy is not None:
                try:
                    pdec_guard = self._policy.classify(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        cwd=cwd,
                        is_elevated=False,
                    )
                except Exception:
                    logger.exception(
                        "policy.classify(cwd=%r, tool=%r) raised during session-grant"
                        " guard; falling back to default ask",
                        cwd,
                        tool_name,
                    )
                    return self._default_decision()
                if pdec_guard.blocked_by_rule:
                    # 危险规则命中 → 强制人审（即使 session grant 也不放行）
                    timeout_ms_guard = (
                        pdec_guard.timeout_ms if pdec_guard.timeout_ms > 0 else 60_000
                    )
                    return _RuleDecision(
                        is_immediate=False,
                        immediate_outcome=None,
                        matched_rule=pdec_guard.blocked_by_rule,
                        severity="standard",
                        auto_approve_at_ms=None,
                        auto_reject_at_ms=None,
                        timeout_ms=timeout_ms_guard,
                    )
            # policy 允许 / policy 缺失 → immediate allow（session grant 命中）；
            # immediate_outcome 用 contract Literal 真源 "approved"
            # （manager.request 把 immediate_outcome 直接当 ApprovalDecision.outcome 用，
            # 必须对齐 :data:`core.contracts.ApprovalOutcome` 而不是 spec 文档漂移的
            # "allowed"——见 approval_manager.py 顶部字面值约定说明）。
            return _RuleDecision(
                is_immediate=True,
                immediate_outcome="approved",
                matched_rule=None,
                severity="standard",
                auto_approve_at_ms=None,
                auto_reject_at_ms=None,
                timeout_ms=60_000,
            )

        # 1. fail-closed：policy 缺失走默认 ask
        if self._policy is None:
            return self._default_decision()

        # 2. 非 generic_chat 通道走默认 ask（claude_code 自走 host_adapter.prompt_approval，
        #    不该进 manager.request → ApprovalRules.classify 这条路径；防御性兜底）
        if channel != "generic_chat":
            return self._default_decision()

        # 3. 委托 policy 决策（fail-closed 包裹）
        try:
            pdec = self._policy.classify(
                tool_name=tool_name,
                tool_input=tool_input,
                cwd=cwd,
                is_elevated=False,
            )
        except Exception:
            logger.exception(
                "policy.classify(cwd=%r, tool=%r) raised; falling back to default ask",
                cwd,
                tool_name,
            )
            return self._default_decision()

        # timeout fallback：policy 返回 ≤0 时降级 60s（用户硬约束）
        timeout_ms = pdec.timeout_ms if pdec.timeout_ms > 0 else 60_000

        # 4. 命中危险规则 → 强制人审（不启 auto-approve）
        if pdec.blocked_by_rule:
            return _RuleDecision(
                is_immediate=False,
                immediate_outcome=None,
                matched_rule=pdec.blocked_by_rule,
                severity="standard",
                auto_approve_at_ms=None,  # 不启 auto-approve
                auto_reject_at_ms=None,
                timeout_ms=timeout_ms,
            )

        # 5. Zap ON + 非危险 + auto_eligible → auto-approve 倒计时
        try:
            enabled_for_cwd = self._policy.is_enabled_for(cwd)
        except Exception:
            logger.exception(
                "policy.is_enabled_for(cwd=%r) raised; falling back to default ask",
                cwd,
            )
            return self._default_decision()

        if pdec.auto_eligible and enabled_for_cwd:
            now_ms = int(time.time() * 1000)
            return _RuleDecision(
                is_immediate=False,
                immediate_outcome=None,
                matched_rule=None,
                severity="standard",
                auto_approve_at_ms=now_ms + timeout_ms,
                auto_reject_at_ms=None,
                timeout_ms=timeout_ms,
            )

        # 6. Zap OFF / 非 auto_eligible → 默认 ask + 60s
        return self._default_decision()

    def add_session_grant(
        self,
        *,
        channel: str,
        thread_id: str,
        cwd: str,
        tool_name: str,
    ) -> None:
        """用户点弹卡「本 session 都同意」后调用，向 thread 级 overrides 加规则。

        当前仅 generic_chat 通道用——claude_code 走 WebHostAdapter 直调 policy，
        v2-inbox 老路径有自己的 grant store；其他通道（cron / evolution / cli）
        阶段 3-4 接入时按需扩展。

        语义：``(cwd, tool_name)`` 写入 ``_thread_overrides[thread_id]``；下次
        同 thread + cwd + tool 的 :meth:`classify` 命中此集 → immediate allow
        （但仍查 ``policy.blocked_by_rule``，危险规则优先级最高）。

        Args:
            channel: 通道名；非 generic_chat 时静默 no-op（防御性边界）
            thread_id: 通道内 thread 标识
            cwd: 触发审批的工作目录
            tool_name: 工具名（如 ``"Bash"`` / ``"Edit"``）
        """
        if channel != "generic_chat":
            # 防御性：其他通道暂不支持 session grant，避免误写污染
            return
        if not thread_id or not cwd or not tool_name:
            # 防御性：空 key 不写入（不可能反查命中也是污染）
            return
        self._thread_overrides.setdefault(thread_id, set()).add((cwd, tool_name))

    def clear_thread_grants(self, thread_id: str) -> int:
        """thread 销毁（cancel_by_thread / cell evict）时清该 thread 所有 grant。

        与 :meth:`ApprovalManager.cancel_by_thread` 同款生命周期，防止
        thread 关闭后 grant 残留在 ``_thread_overrides`` 造成内存泄漏
        （R10 同款风险）。

        Args:
            thread_id: 要清理的 thread

        Returns:
            清掉的 (cwd, tool_name) 条目数；thread_id 不在 overrides 时返 0
        """
        return len(self._thread_overrides.pop(thread_id, set()))

    # ----- 内部 -----

    @staticmethod
    def _default_decision() -> _RuleDecision:
        """默认 ask + 60s（fail-closed 兜底）。"""
        return _RuleDecision(
            is_immediate=False,
            immediate_outcome=None,
            matched_rule=None,
            severity="standard",
            auto_approve_at_ms=None,
            auto_reject_at_ms=None,
            timeout_ms=60_000,
        )


__all__ = ["ApprovalRules", "_RuleDecision"]
