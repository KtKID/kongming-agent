"""WebAutoApprovalManager 装配测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hosts.web.app_support.auto_approval_manager import WebAutoApprovalManager


def test_build_creates_safety_manager_and_audit(tmp_path: Path) -> None:
    manager = WebAutoApprovalManager.build(tmp_path)

    assert manager.safety_manager.root_dir == tmp_path / "web" / "auto_approval"
    assert manager.safety_manager.rules_path.exists()
    assert manager.audit.path == manager.safety_manager.root_dir / "audit.jsonl"
    assert manager.audit.path.exists()


def test_attach_to_app_state_sets_manager_and_compat_fields(tmp_path: Path) -> None:
    manager = WebAutoApprovalManager.build(tmp_path)
    app = SimpleNamespace(state=SimpleNamespace())

    installed = manager.attach_to_app_state(app)

    assert installed is manager
    assert app.state.web_auto_approval_manager is manager
    assert app.state.auto_approval_manager is manager.safety_manager
    assert app.state.auto_approval_policy is manager.safety_manager.policy
    assert app.state.auto_approval_audit is manager.audit
