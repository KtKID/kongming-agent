"""AutoApprovalManager 边界测试。"""

from __future__ import annotations

from pathlib import Path

from safety.auto_approval import ApprovalDispositionMode, AutoApprovalManager


def test_build_materializes_rules_and_exposes_policy(tmp_path: Path) -> None:
    manager = AutoApprovalManager.build(tmp_path)

    assert manager.root_dir == tmp_path / "web" / "auto_approval"
    assert manager.rules_path == manager.root_dir / "rules.yaml"
    assert manager.rules_path.exists()
    assert manager.policy.config_store is manager.config_store
    assert manager.policy.rule_set is manager.rule_set


def test_manager_delegates_mode_config(tmp_path: Path) -> None:
    manager = AutoApprovalManager.build(tmp_path)

    cfg = manager.set_mode("/proj", ApprovalDispositionMode.LLM)
    assert cfg.mode is ApprovalDispositionMode.LLM
    assert manager.mode_for("/proj") is ApprovalDispositionMode.LLM
    assert manager.get_config("/proj").mode is ApprovalDispositionMode.LLM

    assert not hasattr(manager, "classify")
