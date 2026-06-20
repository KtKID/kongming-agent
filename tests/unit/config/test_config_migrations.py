"""配置 schema version 迁移测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from infrastructure.config import CURRENT_CONFIG_SCHEMA_VERSION, load_config
from infrastructure.config.errors import ConfigLoadError
from infrastructure.config.migrations import migrate_config_if_needed

_LEGACY_YAML = """\
# legacy config
model:
  # keep model name comment
  name: local

  base_url: http://127.0.0.1:1234/v1
web:
  dashboard_poll_interval_seconds: 9
"""


def _write_legacy_config(tmp_path: Path, text: str = _LEGACY_YAML) -> Path:
    """写入旧版配置，返回配置路径。"""
    path = tmp_path / "setting.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_migrates_unversioned_yaml_to_v05(tmp_path: Path) -> None:
    """缺版本旧配置通过正式入口加载后，会写回 v0.5 和迁移清单字段。"""
    config_path = _write_legacy_config(tmp_path)

    cfg = load_config(config_path, load_env_file=False)

    assert cfg.config_schema_version == CURRENT_CONFIG_SCHEMA_VERSION
    text = config_path.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith(
        f"config_schema_version: {CURRENT_CONFIG_SCHEMA_VERSION}"
    )
    assert "配置结构版本；当前版本为 v0.5。" in text
    raw = yaml.safe_load(text)
    assert raw["config_schema_version"] == CURRENT_CONFIG_SCHEMA_VERSION
    assert raw["web"]["ws_heartbeat_interval_ms"] == 30000
    assert raw["web"]["deep_research_source_provider"]["enabled"] is True
    assert "api.moonshot.cn" not in text
    assert "provider_routing:" not in text
    assert "\nworkflow:" not in text
    assert "\nsitian:" not in text
    assert "前端 WebSocket 前台 ping 间隔" in text
    assert "Deep Research 来源工具配置" in text


def test_migration_only_adds_version_for_config_without_web_section(tmp_path: Path) -> None:
    """无 web 段的旧配置只写版本号，不创建无关 section。"""
    config_path = _write_legacy_config(
        tmp_path,
        """\
model:
  name: local
  base_url: http://127.0.0.1:1234/v1
""",
    )

    result = migrate_config_if_needed(config_path)

    text = config_path.read_text(encoding="utf-8")
    assert result.added_fields == ("config_schema_version",)
    assert "web:" not in text
    assert "provider_routing:" not in text
    assert "\nworkflow:" not in text
    assert "\nsitian:" not in text


def test_migration_preserves_user_values_comments_and_blank_lines(tmp_path: Path) -> None:
    """迁移只补缺失字段，用户显式值、注释和空行保留。"""
    config_path = _write_legacy_config(tmp_path)
    original_text = config_path.read_text(encoding="utf-8")

    load_config(config_path, load_env_file=False)

    migrated = config_path.read_text(encoding="utf-8")
    assert "# legacy config" in migrated
    assert "# keep model name comment" in migrated
    assert "dashboard_poll_interval_seconds: 9" in migrated
    assert migrated.count("\n\n") >= original_text.count("\n\n")


def test_migration_is_idempotent_for_current_version(tmp_path: Path) -> None:
    """已迁移配置再次检查不应继续改写文件。"""
    config_path = _write_legacy_config(tmp_path)
    first = migrate_config_if_needed(config_path)
    migrated_text = config_path.read_text(encoding="utf-8")

    second = migrate_config_if_needed(config_path)

    assert first.migrated is True
    assert first.source_version == "v0"
    assert "config_schema_version" in first.added_fields
    assert "model.provider_routing" not in first.added_fields
    assert second.migrated is False
    assert second.source_version == CURRENT_CONFIG_SCHEMA_VERSION
    assert config_path.read_text(encoding="utf-8") == migrated_text


def test_migration_rejects_unknown_schema_version(tmp_path: Path) -> None:
    """未知 schema version 直接报配置加载错误。"""
    config_path = _write_legacy_config(
        tmp_path,
        """\
config_schema_version: v9
model:
  name: local
  base_url: http://127.0.0.1:1234/v1
""",
    )

    with pytest.raises(ConfigLoadError):
        load_config(config_path, load_env_file=False)
