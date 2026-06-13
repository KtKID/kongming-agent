"""自动审批模块门户。

`AutoApprovalManager` 是 `safety.auto_approval` 对外的任务级入口，负责
物化规则、装配配置仓库与 policy，并提供 classify / toggle / query 能力。
调用方通过本类使用自动审批，模块内部的 policy / rules / config_store 保持为
实现细节。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from safety.auto_approval.config_store import ConfigStore, ProjectConfig
from safety.auto_approval.policy import AutoApprovalPolicy, Decision
from safety.auto_approval.rules import (
    RuleSet,
    load_default_rules,
    materialize_user_rules_yaml,
)


@dataclass(frozen=True, slots=True)
class AutoApprovalManager:
    """自动审批模块边界类。"""

    policy: AutoApprovalPolicy
    config_store: ConfigStore
    rule_set: RuleSet
    root_dir: Path
    rules_path: Path

    @classmethod
    def build(cls, home: Path) -> AutoApprovalManager:
        """从 kongming home 装配自动审批完整能力。"""
        root_dir = Path(home) / "web" / "auto_approval"
        root_dir.mkdir(parents=True, exist_ok=True)
        rules_path = materialize_user_rules_yaml(Path(home))
        config_store = ConfigStore(root_dir)
        rule_set = load_default_rules(rules_path)
        policy = AutoApprovalPolicy(rule_set, config_store)
        return cls(
            policy=policy,
            config_store=config_store,
            rule_set=rule_set,
            root_dir=root_dir,
            rules_path=rules_path,
        )

    def classify(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any] | Any,
        cwd: str,
        is_elevated: bool,
    ) -> Decision:
        """判断一次工具调用是否可进入自动审批流程。"""
        return self.policy.classify(
            tool_name=tool_name,
            tool_input=tool_input,
            cwd=cwd,
            is_elevated=is_elevated,
        )

    def is_enabled_for(self, cwd: str) -> bool:
        """查询指定 cwd 是否启用智能审批。"""
        return self.policy.is_enabled_for(cwd)

    def get_config(self, cwd: str) -> ProjectConfig:
        """读取指定 cwd 的自动审批配置。"""
        return self.policy.get_config(cwd)

    def set_enabled(self, cwd: str, enabled: bool) -> ProjectConfig:
        """设置指定 cwd 的智能审批总开关。"""
        return self.policy.set_enabled(cwd, enabled)


__all__ = ["AutoApprovalManager"]
