"""AutoApprovalManager 边界测试。"""

from __future__ import annotations

from pathlib import Path

from safety.auto_approval import AutoApprovalManager


def test_build_materializes_rules_and_exposes_policy(tmp_path: Path) -> None:
    manager = AutoApprovalManager.build(tmp_path)

    assert manager.root_dir == tmp_path / "web" / "auto_approval"
    assert manager.rules_path == manager.root_dir / "rules.yaml"
    assert manager.rules_path.exists()
    assert manager.policy.config_store is manager.config_store
    assert manager.policy.rule_set is manager.rule_set


def test_manager_delegates_config_and_classify(tmp_path: Path) -> None:
    manager = AutoApprovalManager.build(tmp_path)

    cfg = manager.set_enabled("/proj", True)
    assert cfg.enabled is True
    assert manager.is_enabled_for("/proj") is True
    assert manager.get_config("/proj").enabled is True

    decision = manager.classify(
        tool_name="Bash",
        tool_input={"command": "ls"},
        cwd="/proj",
        is_elevated=False,
    )
    assert decision.auto_eligible is True
    assert decision.blocked_by_rule is None
