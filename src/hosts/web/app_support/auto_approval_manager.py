"""Web 侧自动审批装配入口。

`WebAutoApprovalManager` 负责把 safety 自动审批门户和 Web 审计日志接入
FastAPI app.state。Web app factory 只依赖本模块，避免直接装配
`safety.auto_approval` 的内部零件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hosts.web.approvals.auto.audit import AuditLogger
from safety.auto_approval.manager import AutoApprovalManager


@dataclass(frozen=True, slots=True)
class WebAutoApprovalManager:
    """Web 宿主的自动审批装配边界。"""

    safety_manager: AutoApprovalManager
    audit: AuditLogger

    @classmethod
    def build(cls, home: Path) -> WebAutoApprovalManager:
        """从 kongming home 构造 Web 自动审批装配对象。"""
        safety_manager = AutoApprovalManager.build(home)
        audit = AuditLogger(safety_manager.root_dir / "audit.jsonl")
        return cls(safety_manager=safety_manager, audit=audit)

    @property
    def policy(self) -> Any:
        """兼容现有 route / tests 的 policy 访问。"""
        return self.safety_manager.policy

    @property
    def config_store(self) -> Any:
        """兼容需要共享 ConfigStore 的装配路径。"""
        return self.safety_manager.config_store

    def attach_to_app_state(self, app: Any) -> WebAutoApprovalManager:
        """把自动审批能力挂到 FastAPI app.state。"""
        app.state.auto_approval_manager = self.safety_manager
        app.state.web_auto_approval_manager = self
        app.state.auto_approval_policy = self.safety_manager.policy
        app.state.auto_approval_audit = self.audit
        return self

    def install(self, app: Any) -> WebAutoApprovalManager:
        """`attach_to_app_state` 的命名别名，方便装配语义阅读。"""
        return self.attach_to_app_state(app)


__all__ = ["WebAutoApprovalManager"]
