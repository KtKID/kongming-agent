"""智能审批决策核心（policy）。

唯一对外 API：``AutoApprovalPolicy.classify(...) → Decision``

依赖方向（单向）::

    rules.py ────► policy.py ◄──── config_store.py
                       ▲
                       │
                  matchers.py

不感知 ApprovalBridge / WS / Claude SDK。可独立单测。

elevated 硬约束：``classify`` **第一行**短路 elevated 路径，永远返回
``auto_eligible=False``，**不读 yaml 不查 config**。这是防御性设计：
即使配置文件被改、yaml 加载出错，elevated 模式也能保证强制人审。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from web.approvals.auto import matchers
from web.approvals.auto.config_store import ConfigStore, ProjectConfig
from web.approvals.auto.rules import RuleDefinition, RuleSet

# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Decision:
    """policy.classify 的结果。

    ``auto_eligible``: True = 走自动通过倒计时；False = 必须人审
    ``blocked_by_rule``: 命中的规则 id（用于前端 banner）；None = 未命中
    ``timeout_ms``: 倒计时毫秒（即使 auto_eligible=False 也返回，前端忽略）
    ``rule_evaluation``: 给 audit 用的完整评估快照
    """

    auto_eligible: bool
    blocked_by_rule: str | None
    timeout_ms: int
    rule_evaluation: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# AutoApprovalPolicy
# ---------------------------------------------------------------------------


class AutoApprovalPolicy:
    """智能审批决策器。

    生命周期：进程级单例（启动加载 RuleSet 后常驻；config_store 每次 classify
    重新读盘，保证 UI toggle 后立即生效）。

    Args:
        rule_set: 启动时加载好的规则集
        config_store: per-cwd 配置仓库
    """

    def __init__(self, rule_set: RuleSet, config_store: ConfigStore) -> None:
        self._rule_set = rule_set
        self._config_store = config_store

    # ----- 公共接口 -----

    @property
    def rule_set(self) -> RuleSet:
        return self._rule_set

    @property
    def config_store(self) -> ConfigStore:
        """暴露 per-cwd 配置仓库（smart-approval-generic-chat-autoallow task #6）。

        让 generic_chat 通道的 :class:`safety.approval_rules.ApprovalRules` 复用
        **同一份** :class:`ConfigStore` 实例——保证 "用户在 UI 一处 toggle 即时
        生效于所有通道（claude_code + generic_chat）"，而不是各通道各持一份
        store / 读不同盘文件。装配点见 ``src/web/run.py``。

        通过 property 暴露而非直接 ``_config_store`` 私有访问：保留封装边界 +
        可测试性（测试 ``policy.config_store is store_instance``）。
        """
        return self._config_store

    def is_enabled_for(self, cwd: str) -> bool:
        """该 cwd 是否启用了智能审批（仅 enabled 开关，不判断规则）。"""
        cfg = self._config_store.get_or_default(cwd)
        return cfg.enabled

    def get_config(self, cwd: str) -> ProjectConfig:
        """读取（不存在则返回默认）。"""
        return self._config_store.get_or_default(cwd)

    def set_enabled(self, cwd: str, enabled: bool) -> ProjectConfig:
        """toggle 总开关 + 持久化。返回最新配置。"""
        cfg = self._config_store.get_or_default(cwd)
        cfg.enabled = enabled
        if not cfg.cwd:
            cfg.cwd = cwd
        self._config_store.set(cfg)
        return cfg

    def classify(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any] | Any,
        cwd: str,
        is_elevated: bool,
    ) -> Decision:
        """核心决策。

        语义::

            if is_elevated:                       → 永远必须人审（硬约束）
            elif not policy_enabled_for(cwd):     → 走原审批（不自动通过）
            elif matched any enabled rule:        → blocked，必须人审
            else:                                 → auto_eligible，倒计时自动通过

        Returns:
            Decision（含 audit 用的 rule_evaluation 快照）
        """
        # === 第一行硬约束：elevated 永不自动 ===
        if is_elevated:
            return Decision(
                auto_eligible=False,
                blocked_by_rule=None,
                timeout_ms=self._rule_set.default_timeout_ms,
                rule_evaluation={
                    "matched": None,
                    "all_rules": [],
                    "reason": "elevated_hard_constraint",
                },
            )

        # === 读 cwd 配置 ===
        cfg = self._config_store.get_or_default(cwd)
        timeout_ms = cfg.timeout_ms if cfg.timeout_ms > 0 else self._rule_set.default_timeout_ms

        # === 不启用智能审批：走原审批，不倒计时 ===
        if not cfg.enabled:
            return Decision(
                auto_eligible=False,
                blocked_by_rule=None,
                timeout_ms=timeout_ms,
                rule_evaluation={
                    "matched": None,
                    "all_rules": [],
                    "reason": "policy_disabled_for_cwd",
                },
            )

        # === 启用智能审批：遍历规则 ===
        active_rules = self._active_rules(cfg)
        active_ids = [r.id for r in active_rules]

        for rule in active_rules:
            if matchers.matches(rule.match, tool_name, tool_input):
                return Decision(
                    auto_eligible=False,
                    blocked_by_rule=rule.id,
                    timeout_ms=timeout_ms,
                    rule_evaluation={
                        "matched": rule.id,
                        "all_rules": active_ids,
                    },
                )

        # 没命中任何规则 → 可以自动通过
        return Decision(
            auto_eligible=True,
            blocked_by_rule=None,
            timeout_ms=timeout_ms,
            rule_evaluation={
                "matched": None,
                "all_rules": active_ids,
            },
        )

    # ----- 内部 -----

    def _active_rules(self, cfg: ProjectConfig) -> list[RuleDefinition]:
        """计算本 cwd 实际启用的规则列表。

        合并逻辑：
        - 默认 ``rule.enabled``（来自 yaml）
        - 被 ``cfg.rule_overrides`` 覆盖（True/False 都覆盖，缺失项走默认）

        只返回最终 ``enabled=True`` 的规则。
        """
        overrides = cfg.rule_overrides or {}
        result: list[RuleDefinition] = []
        for rule in self._rule_set.rules:
            effective = overrides.get(rule.id, rule.enabled)
            if effective:
                result.append(rule)
        return result


__all__ = ["AutoApprovalPolicy", "Decision"]
