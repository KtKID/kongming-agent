"""验证 safety v0.6 全局配置与 permissions 值对象合同。

本文件覆盖三类边界：全局配置只保留审批模式与 auto 预留位、旧全局规则
返回定向迁移提示、thread permissions 值对象保持不可变。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from infrastructure.config import load_config
from infrastructure.config.errors import ConfigValidationError
from infrastructure.config.manager import ConfigManager
from infrastructure.config.models import (
    SafetyApprovalLlmConfig,
    SafetyConfig,
)
from infrastructure.config.schema import list_field_metas
from infrastructure.config.writer import PatchItem, ValidationFailedError
from safety.approval.rule_models import (
    MatcherKind,
    PermissionEntry,
    PermissionResolution,
    PermissionRuleRecord,
    RuleMatch,
    ThreadPermissionsSnapshot,
    Verdict,
)


def _write_config(path: Path, safety_yaml: str) -> None:
    """写入本地模型最小配置，并把指定 safety 片段放入测试文件。"""
    path.write_text(
        f"model:\n  preset_id: local-gemma-4-e4b-it\nsafety:\n{safety_yaml}",
        encoding="utf-8",
    )


def test_safety_config_only_exposes_llm_reviewer_config() -> None:
    """SafetyConfig 只持有独立 LLM 复核器配置。"""
    assert set(SafetyConfig.model_fields) == {"approval"}

    cfg = SafetyConfig()

    assert cfg.approval.llm is None


def test_llm_reviewer_config_validates_required_model_fields() -> None:
    """LLM 复核器具备独立连接配置并拒绝空白模型标识。"""
    config = SafetyApprovalLlmConfig(
        model=" reviewer-small ",
        base_url="http://127.0.0.1:11434/v1/",
    )
    assert config.model == "reviewer-small"
    assert config.base_url == "http://127.0.0.1:11434/v1"

    with pytest.raises(ValidationError, match="must not be blank"):
        SafetyApprovalLlmConfig(model="   ", base_url="http://127.0.0.1:11434/v1")


def test_safety_schema_lists_only_llm_reviewer_field() -> None:
    """配置管理 schema 只公开 LLM 复核器对象。"""
    safety_metas = {
        meta.path: meta for meta in list_field_metas() if meta.path.startswith("safety.")
    }

    assert set(safety_metas) == {
        "safety.approval.llm",
    }
    assert safety_metas["safety.approval.llm"].type == "dict"


def test_config_manager_updates_llm_reviewer_config(tmp_path: Path) -> None:
    """ConfigManager 可原子写回 LLM 复核器配置，并由正式 loader 复验。"""
    config_path = tmp_path / "setting.yaml"
    _write_config(
        config_path,
        "  approval:\n    llm:\n",
    )
    manager = ConfigManager(config_path)
    mtime = config_path.stat().st_mtime

    result = manager.save_patch(
        [
            PatchItem(
                path="safety.approval.llm",
                value={
                    "model": "reviewer-small",
                    "base_url": "http://127.0.0.1:11434/v1",
                },
            ),
        ],
        expected_mtime=mtime,
    )
    cfg = load_config(config_path, load_env_file=False, migrate=False)

    assert result.restart_required_fields == [
        "safety.approval.llm",
    ]
    assert cfg.safety.approval.llm is not None
    assert cfg.safety.approval.llm.model == "reviewer-small"


def test_config_manager_rejects_removed_global_permissions_path(tmp_path: Path) -> None:
    """ConfigManager 不创建已退出 schema 的全局 permissions 路径。"""
    config_path = tmp_path / "setting.yaml"
    _write_config(
        config_path,
        "  approval:\n    llm:\n",
    )
    manager = ConfigManager(config_path)

    with pytest.raises(ValidationFailedError) as caught:
        manager.save_patch(
            [PatchItem(path="safety.permissions", value={"allow": [], "deny": []})],
            expected_mtime=config_path.stat().st_mtime,
        )

    assert caught.value.errors == [
        {"path": "safety.permissions", "message": "leaf key not present in yaml"}
    ]


@pytest.mark.parametrize(
    "legacy_field",
    [
        "allow_tools_silent",
        "allow_writes",
        "approval_required_commands",
        "approval_rules",
        "hard_deny_commands",
        "log_silent_reads",
        "permissions",
        "sensitive_paths",
        "skill_call_rules",
        "trusted_workdirs",
    ],
)
def test_loader_rejects_legacy_safety_field_with_targeted_migration(
    tmp_path: Path,
    legacy_field: str,
) -> None:
    """旧全局 safety 字段必须返回带显式 thread 目标的迁移命令。"""
    config_path = tmp_path / "setting.yaml"
    _write_config(config_path, f"  {legacy_field}: []\n")

    with pytest.raises(ConfigValidationError) as caught:
        load_config(config_path, load_env_file=False, migrate=False)

    assert caught.value.details["code"] == "legacy_safety_permissions_require_migration"
    assert caught.value.details["fields"] == [f"safety.{legacy_field}"]
    command = caught.value.details["migration_command"]
    assert "scripts/migrate_permissions_v06.py" in command
    assert "--thread-id <thread-id>" in command
    assert "--dry-run" in command


@pytest.mark.parametrize("legacy_field", ["approval_mode", "auto_judge"])
def test_loader_rejects_legacy_global_approval_disposition(
    tmp_path: Path,
    legacy_field: str,
) -> None:
    """旧全局处置模式必须迁移到每 cwd 的审批选择器。"""
    config_path = tmp_path / "setting.yaml"
    _write_config(config_path, f"  {legacy_field}: full_trust\n")

    with pytest.raises(ConfigValidationError) as caught:
        load_config(config_path, load_env_file=False, migrate=False)

    assert caught.value.details["code"] == "legacy_global_approval_disposition"
    assert caught.value.details["fields"] == [f"safety.{legacy_field}"]


def test_thread_permission_value_objects_are_frozen() -> None:
    """permission entry、resolution 和 snapshot 创建后语义保持稳定。"""
    matcher = RuleMatch(
        kind=MatcherKind.TOOL_EXACT,
        tool_name="read_file",
        pattern="read_file",
        canonical_expression="read_file",
    )
    entry = PermissionEntry(
        rule=PermissionRuleRecord(expression="read_file", scope_cwd=None),
        verdict=Verdict.ALLOW,
        matcher=matcher,
    )
    resolution = PermissionResolution(verdict=Verdict.ALLOW, expression="read_file")
    snapshot = ThreadPermissionsSnapshot(
        thread_id="thread-a",
        revision=1,
        allow=(PermissionRuleRecord(expression="read_file", scope_cwd=None),),
        deny=(),
        updated_at=None,
    )

    assert entry.matcher is matcher
    assert resolution.verdict is Verdict.ALLOW
    assert snapshot.schema_version == 2
    assert snapshot.allow == (PermissionRuleRecord(expression="read_file", scope_cwd=None),)
    with pytest.raises(FrozenInstanceError):
        snapshot.revision = 2  # type: ignore[misc]
