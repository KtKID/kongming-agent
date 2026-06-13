"""Web 自动审批装配 helper。

本模块集中创建自动审批 policy / config store / audit logger，并把实例挂到
FastAPI ``app.state``。``hosts.web.app`` 只调用本装配入口，避免 app shell
直接穿透到 safety 层。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hosts.web.approvals.auto import (
    AuditLogger,
    AutoApprovalPolicy,
    ConfigStore,
    load_default_rules,
    materialize_user_rules_yaml,
)


def configure_auto_approval(app: Any, home: Path) -> None:
    """创建自动审批单例并写入 ``app.state``。

    Args:
        app: FastAPI app 实例；只要求存在 ``state`` 属性。
        home: Kongming home 目录，用于 rules.yaml 物化和 audit JSONL 落盘。
    """

    auto_approval_root = home / "web" / "auto_approval"
    auto_approval_root.mkdir(parents=True, exist_ok=True)
    user_rules_yaml = materialize_user_rules_yaml(home)
    app.state.auto_approval_policy = AutoApprovalPolicy(
        load_default_rules(user_rules_yaml),
        ConfigStore(auto_approval_root),
    )
    app.state.auto_approval_audit = AuditLogger(auto_approval_root / "audit.jsonl")
