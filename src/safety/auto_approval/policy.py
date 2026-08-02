"""默认询问的自动处置模式。

危险规则匹配由 ``DangerGuard`` 统一负责。本模块只保留每个 cwd 的
启用状态和倒计时配置，供 ApprovalManager 处置 ``default:ask``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Literal

from safety.auto_approval.config_store import ConfigStore, ProjectConfig
from safety.auto_approval.disposition import ApprovalDispositionMode
from safety.auto_approval.rules import RuleSet


class AutoApprovalPolicy:
    """每个 cwd 的 default-ask 自动处置配置门户。"""

    def __init__(self, rule_set: RuleSet, config_store: ConfigStore) -> None:
        self._rule_set = rule_set
        self._config_store = config_store

    @property
    def rule_set(self) -> RuleSet:
        """返回默认倒计时配置快照。"""
        return self._rule_set

    @property
    def config_store(self) -> ConfigStore:
        """返回 cwd 配置存储。"""
        return self._config_store

    def mode_for(self, cwd: str) -> ApprovalDispositionMode:
        """返回该 cwd 的 default:ask 处置模式。"""
        return self._config_store.get_or_default(cwd).mode

    def get_config(self, cwd: str) -> ProjectConfig:
        """读取 cwd 配置，不存在时返回默认值。"""
        return self._config_store.get_or_default(cwd)

    def set_mode(self, cwd: str, mode: ApprovalDispositionMode) -> ProjectConfig:
        """持久化 cwd 处置模式并返回最新配置。"""
        config = self._config_store.get_or_default(cwd)
        updated = replace(config, cwd=cwd, mode=mode)
        self._config_store.set(updated)
        return updated

    def set_mode_from_wire(self, cwd: str, raw_mode: str) -> ProjectConfig:
        """解析 Web wire 枚举并持久化每 cwd 处置模式。"""
        return self.set_mode(cwd, ApprovalDispositionMode(raw_mode))

    def state_for_wire(
        self,
        cwd: str,
    ) -> tuple[Literal["user", "llm", "full_trust"], int, Mapping[str, bool]]:
        """提供 Web 处置模式 state，隔离宿主对内部配置类型的依赖。"""
        config = self.get_config(cwd)
        timeout_ms = config.timeout_ms or self._rule_set.default_timeout_ms
        return config.mode.value, timeout_ms, config.rule_overrides


__all__ = ["AutoApprovalPolicy"]
