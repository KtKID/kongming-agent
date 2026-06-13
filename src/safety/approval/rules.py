"""审批策略层容器（approval-rules-unified：多通道复用统一自动审批策略）。

审批管理器调用 ``classify(channel, thread_id, cwd, tool_name, tool_input)`` 拿初步决策。

设计目标：**一份规则，多通道共用**——让 generic_chat / cli 通道复用
:class:`safety.auto_approval.policy.AutoApprovalPolicy` + ``default_rules.yaml``
+ 每工作目录 :class:`safety.auto_approval.config_store.ConfigStore`。

设计真源：
- ``dev-pipeline/tasks/approval-rules-unified/README.md`` — 技术设计 + 完成标准
- ``docs/safety-approval-manager-v0.5/30-rules-schema.md`` — 长期 schema 演进

鸭子类型边界（safety 不直接 import web）：
通过 :class:`_AutoApprovalPolicyProto` + :class:`_PolicyDecisionLike` Protocol
注入 :class:`AutoApprovalPolicy` 实例；装配点在 ``src/hosts/web/run.py`` 和
``src/hosts/cli/main.py``。

claude_code 通道仍走 :meth:`web.app_support.host_adapter.WebHostAdapter.prompt_approval`
直调策略，**不经** ApprovalRules。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_AUTO_APPROVAL_CHANNELS: frozenset[str] = frozenset({"generic_chat", "cli"})
_SESSION_GRANT_CHANNELS: frozenset[str] = frozenset({"generic_chat"})


@dataclass
class _RuleDecision:
    """rules.classify 的返回类型。

    字段:
        is_immediate: True = 跳过用户直接决策（规则立即决策）；
            False = 需要显式同意（审批管理器创建待处理 future）
        immediate_outcome: is_immediate=True 时的结果（'approved' / 'rejected'）；
            is_immediate=False 时无意义
        matched_rule: 命中的规则 ID（如 ``"bash_rm_any"``）；未命中 = None
        severity: ``'standard'`` / ``'elevated'``（用于收件箱载荷 + UI 渲染）
        auto_approve_at_ms: 安全路径倒计时到点 ms；非自动通过路径恒 None
        auto_reject_at_ms: 危险路径倒计时到点 ms；非自动拒绝路径恒 None
        timeout_ms: 用户决策的超时阈值；兜底 60_000（与 WebHostAdapter._timeout 默认一致）
    """

    is_immediate: bool
    immediate_outcome: str | None
    matched_rule: str | None
    severity: str
    auto_approve_at_ms: int | None
    auto_reject_at_ms: int | None
    timeout_ms: int | None


# ---------------------------------------------------------------------------
# 鸭子类型协议（避免 safety → web 跨层 import，import-linter 合约强制）
# ---------------------------------------------------------------------------


class _PolicyDecisionLike(Protocol):
    """对 :class:`safety.auto_approval.policy.Decision` 的鸭子类型协议。

    仅消费 ``auto_eligible`` / ``blocked_by_rule`` / ``timeout_ms`` 三字段
    （``rule_evaluation`` 审计快照本层不消费）。

    实际真源签名：``src/safety/auto_approval/policy.py:33-46`` —— ``Decision`` 是
    ``@dataclass(frozen=True, slots=True)``，字段以属性暴露，匹配本 Protocol
    即可（无需显式继承）。
    """

    @property
    def auto_eligible(self) -> bool: ...

    @property
    def blocked_by_rule(self) -> str | None: ...

    @property
    def timeout_ms(self) -> int: ...


class _AutoApprovalPolicyProto(Protocol):
    """对 :class:`safety.auto_approval.policy.AutoApprovalPolicy` 的鸭子类型协议。

    仅消费 ``classify`` + ``is_enabled_for`` 两方法（``set_enabled`` / ``get_config``
    等 UI 写入路径不在 safety 层使用）。

    实际真源签名：``src/safety/auto_approval/policy.py:107-114``::

        def classify(
            self,
            *,
            tool_name: str,
            tool_input: dict[str, Any],
            cwd: str,
            is_elevated: bool,
        ) -> Decision: ...

    与 :class:`safety.inbox.event_sink._BroadcasterProto` 同款解耦模式
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
    """统一规则代理层——把 generic_chat / cli 通道的 ``classify`` 委托给注入的
    :class:`AutoApprovalPolicy` 实例。

    架构：完整复用策略的 ``classify``（含 24 条规则匹配 + cwd 开关 +
    审计快照），不再仅查 cwd 总开关 + timeout。

    历史变更（approval-rules-unified）：见 changelog；旧 Protocol / 字段架构
    已废弃，不在 source 文档保留旧名字（避免外部脆弱 grep 误匹配）。

    失败关闭设计：
    ``policy=None`` 时（测试环境 / 生命周期漂移）走默认 ask + 60s，
    保证审批主流程在配置缺失时仍可阻塞等用户决策（**不开自动通过倒计时**）。

    阶段范围：
    - 当前：generic_chat / cli 接入共享策略；其他通道
      （cron / evolution / claude_code）恒走默认 ask + 60s
      （claude_code 自走 WebHostAdapter.prompt_approval）
    - 阶段 5（spec 演进）：rules.yaml 完整 schema + per-thread + session grants
    """

    def __init__(
        self,
        *,
        policy: _AutoApprovalPolicyProto | None = None,
    ) -> None:
        """构造 ApprovalRules（注入可选 AutoApprovalPolicy）。

        Args:
            policy: 智能审批决策器实例（鸭子类型真源
                :class:`safety.auto_approval.policy.AutoApprovalPolicy`）；
                ``None`` = 失败关闭走默认 ask（测试环境 / app.state 未就绪时
                的安全网）。
        """
        self._policy = policy
        # manager-session-grant：thread 级本次会话同意覆盖；
        # key = thread_id；value = set of (cwd, tool_name) 元组。
        # - 用户点弹卡「本次会话都同意」→ ApprovalManager.resolve 调
        #   add_session_grant 写入；
        # - 下次同 thread + cwd + tool 的 classify 命中此集 → 立即允许
        #   （但仍要查策略的 blocked_by_rule，危险规则优先级最高）；
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

            if policy is None:                     → 失败关闭默认 ask + 60s
            elif channel not in {'generic_chat','cli'}:
                                                     → 默认 ask + 60s
            elif pdec.blocked_by_rule:             → 危险待审 + 超时自动拒绝（matched_rule 透传）
            elif pdec.auto_eligible and enabled:   → 自动通过倒计时（timeout 兜底 60s）
            else:                                  → 默认 ask + 60s

        ``is_elevated`` 阶段 1 generic_chat 都按 ``False``（spec 阶段 5 才区分；
        codex / cli 后续接入时再处理）。

        **失败关闭兜底**：``policy.classify`` 抛任何异常时降级走默认 ask + 60s，
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
        # 0. thread 级本次会话授权（manager-session-grant）：
        #    用户在此 thread 之前点过「本次会话都同意」→ 命中后**仍要查
        #    策略的 blocked_by_rule**，危险规则（rm 等）优先级最高，绝不允许
        #    本次会话授权绕过守护；策略缺失时无法检查危险规则，默认 ask。
        if channel in _SESSION_GRANT_CHANNELS and (
            cwd,
            tool_name,
        ) in self._thread_overrides.get(thread_id, set()):
            if self._policy is None:
                return self._default_decision()

            # 仍要查策略是否命中 blocked_by_rule（防止本次会话授权绕过 rm 守护）
            try:
                pdec_guard = self._policy.classify(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    cwd=cwd,
                    is_elevated=False,
                )
            except Exception:
                logger.exception(
                    "本次会话授权守卫调用 policy.classify(cwd=%r, tool=%r) 失败，回退默认 ask",
                    cwd,
                    tool_name,
                )
                return self._default_decision()
            if pdec_guard.blocked_by_rule:
                # 危险规则命中 → 等待用户明确允许；超时按规则默认拒绝。
                timeout_ms_guard = pdec_guard.timeout_ms if pdec_guard.timeout_ms > 0 else 60_000
                return self._blocked_decision(
                    matched_rule=pdec_guard.blocked_by_rule,
                    timeout_ms=timeout_ms_guard,
                )

            # 策略允许 → 立即允许（本次会话授权命中）；
            # immediate_outcome 用合约字面值真源 "approved"
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

        # 1. 失败关闭：policy 缺失走默认 ask
        if self._policy is None:
            return self._default_decision()

        # 2. 未接入共享自动审批的通道走默认 ask（claude_code 自走
        #    host_adapter.prompt_approval；cron / evolution 暂未接入）。
        if channel not in _AUTO_APPROVAL_CHANNELS:
            return self._default_decision()

        # 3. 委托 policy 决策（失败关闭包裹）
        try:
            pdec = self._policy.classify(
                tool_name=tool_name,
                tool_input=tool_input,
                cwd=cwd,
                is_elevated=False,
            )
        except Exception:
            logger.exception(
                "policy.classify(cwd=%r, tool=%r) 失败，回退默认 ask",
                cwd,
                tool_name,
            )
            return self._default_decision()

        # timeout 兜底：策略返回 ≤0 时降级 60s（用户硬约束）
        timeout_ms = pdec.timeout_ms if pdec.timeout_ms > 0 else 60_000

        # 4. 命中危险规则 → 等待用户明确允许；超时按规则默认拒绝。
        if pdec.blocked_by_rule:
            return self._blocked_decision(
                matched_rule=pdec.blocked_by_rule,
                timeout_ms=timeout_ms,
            )

        # 5. 总开关开启 + 非危险 + auto_eligible → 自动允许倒计时
        try:
            enabled_for_cwd = self._policy.is_enabled_for(cwd)
        except Exception:
            logger.exception(
                "policy.is_enabled_for(cwd=%r) 失败，回退默认 ask",
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

        # 6. 总开关关闭 / 非 auto_eligible → 默认 ask + 60s
        return self._default_decision()

    def add_session_grant(
        self,
        *,
        channel: str,
        thread_id: str,
        cwd: str,
        tool_name: str,
    ) -> None:
        """用户点弹卡「本次会话都同意」后调用，向 thread 级覆盖加规则。

        仅 generic_chat 写入。CLI 终端交互只支持单次允许 / 拒绝，
        claude_code 走 WebHostAdapter 直调 policy；cron / evolution 后续接入时再扩展。

        语义：``(cwd, tool_name)`` 写入 ``_thread_overrides[thread_id]``；下次
        同 thread + cwd + tool 的 :meth:`classify` 命中此集 → 立即允许
        （但仍查策略的 ``blocked_by_rule``，危险规则优先级最高）。

        Args:
            channel: 通道名；非托管自动审批通道时静默 no-op（防御性边界）
            thread_id: 通道内 thread 标识
            cwd: 触发审批的工作目录
            tool_name: 工具名（如 ``"Bash"`` / ``"Edit"``）
        """
        if channel not in _SESSION_GRANT_CHANNELS:
            # 防御性：未接入共享自动审批的通道不写入审批管理器授权
            return
        if not thread_id or not cwd or not tool_name:
            # 防御性：空 key 不写入（不可能反查命中也是污染）
            return
        self._thread_overrides.setdefault(thread_id, set()).add((cwd, tool_name))

    def clear_thread_grants(self, thread_id: str) -> int:
        """thread 销毁（cancel_by_thread / cell evict）时清该 thread 所有授权。

        与 :meth:`ApprovalManager.cancel_by_thread` 同款生命周期，防止
        thread 关闭后授权残留在 ``_thread_overrides`` 造成内存泄漏
        （R10 同款风险）。

        Args:
            thread_id: 要清理的 thread

        Returns:
            清掉的 (cwd, tool_name) 条目数；thread_id 不在覆盖表时返 0
        """
        return len(self._thread_overrides.pop(thread_id, set()))

    # ----- 内部 -----

    @staticmethod
    def _blocked_decision(*, matched_rule: str, timeout_ms: int) -> _RuleDecision:
        """危险规则命中：人工可显式允许，超时默认拒绝。"""
        now_ms = int(time.time() * 1000)
        return _RuleDecision(
            is_immediate=False,
            immediate_outcome=None,
            matched_rule=matched_rule,
            severity="elevated",
            auto_approve_at_ms=None,
            auto_reject_at_ms=now_ms + timeout_ms,
            timeout_ms=timeout_ms,
        )

    @staticmethod
    def _default_decision() -> _RuleDecision:
        """默认 ask + 60s（失败关闭兜底）。"""
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
