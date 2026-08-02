"""配置 schema version 迁移测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from infrastructure.config import CURRENT_CONFIG_SCHEMA_VERSION, load_config
from infrastructure.config.errors import ConfigLoadError, ConfigValidationError
from infrastructure.config.migrations import migrate_config_if_needed
from infrastructure.config.schema import get_field_meta

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


def test_load_config_migrates_unversioned_yaml_to_v06(tmp_path: Path) -> None:
    """缺版本旧配置通过正式入口加载后，会写回 v0.6 和迁移清单字段。"""
    config_path = _write_legacy_config(tmp_path)

    cfg = load_config(config_path, load_env_file=False)

    assert cfg.config_schema_version == CURRENT_CONFIG_SCHEMA_VERSION
    text = config_path.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith(
        f"config_schema_version: {CURRENT_CONFIG_SCHEMA_VERSION}"
    )
    assert "配置结构版本；当前版本为 v0.6。" in text
    raw = yaml.safe_load(text)
    assert raw["config_schema_version"] == CURRENT_CONFIG_SCHEMA_VERSION
    assert "enabled" not in raw["mcp"]
    assert raw["mcp"]["servers"][0]["server_id"] == "minimax"
    assert raw["mcp"]["servers"][0]["command"] == "uvx"
    assert raw["web_search"]["enabled"] is True
    assert raw["web_search"]["search_tool_name"] == "mcp__minimax__web_search"
    assert raw["web"]["ws_heartbeat_interval_ms"] == 30000
    assert raw["web"]["deep_research_source_provider"]["enabled"] is True
    assert "api.moonshot.cn" not in text
    assert raw["model"]["preset_id"].startswith("custom-model-")
    assert set(raw["model"]) == {"preset_id", "reasoning_effort"}
    assert "\nworkflow:" not in text
    assert "\nsitian:" not in text
    assert "前端 WebSocket 前台 ping 间隔" in text
    assert "Deep Research 来源工具配置" in text


def test_migration_without_web_section_only_converts_model_and_version(tmp_path: Path) -> None:
    """无 web 段的旧配置只迁移版本与模型，不创建无关 section。"""
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
    assert "config_schema_version" in result.added_fields
    assert "model.preset_id" in result.added_fields
    assert "model.name" in result.removed_fields
    assert "model.base_url" in result.removed_fields
    assert "web:" not in text
    assert "provider_routing:" not in text
    assert "\nworkflow:" not in text
    assert "\nsitian:" not in text
    assert "\nmcp:" not in text
    assert "\nweb_search:" not in text


def test_migration_preserves_user_values_and_comments(tmp_path: Path) -> None:
    """迁移保留未迁移 section 的用户值与 model 字段注释。"""
    config_path = _write_legacy_config(tmp_path)

    load_config(config_path, load_env_file=False)

    migrated = config_path.read_text(encoding="utf-8")
    assert "# legacy config" in migrated
    assert "# keep model name comment" in migrated
    assert "dashboard_poll_interval_seconds: 9" in migrated


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


def test_migration_removes_legacy_web_origin_field_from_current_yaml(tmp_path: Path) -> None:
    """当前版本配置残留旧 Web origin 字段时会被迁移层删除。"""
    legacy_key = "public_origin"
    config_path = _write_legacy_config(
        tmp_path,
        f"""\
config_schema_version: {CURRENT_CONFIG_SCHEMA_VERSION}
model:
  preset_id: local-gemma-4-e4b-it
web:
  server_origin: http://192.168.31.23:8765
  {legacy_key}: http://192.168.31.23:8765
""",
    )

    result = migrate_config_if_needed(config_path)
    cfg = load_config(config_path, load_env_file=False)

    assert result.migrated is True
    assert result.removed_fields == (f"web.{legacy_key}",)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert legacy_key not in raw["web"]
    assert cfg.web.server_origin == "http://192.168.31.23:8765"


def test_load_config_removes_retired_web_approval_timeout_field(
    tmp_path: Path,
) -> None:
    """用户配置残留旧审批超时字段时，load_config 会先迁移删除再校验。"""
    legacy_key = "pending_approval_timeout_seconds"
    config_path = _write_legacy_config(
        tmp_path,
        f"""\
config_schema_version: {CURRENT_CONFIG_SCHEMA_VERSION}
model:
  preset_id: local-gemma-4-e4b-it
web:
  enabled: true
  server_origin:
  {legacy_key}: 60
""",
    )

    cfg = load_config(config_path, load_env_file=False)

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert legacy_key not in raw["web"]
    assert cfg.web.enabled is True


def test_load_config_removes_retired_mcp_global_switch(tmp_path: Path) -> None:
    """用户级配置残留旧 MCP 总开关时，迁移层会删除该字段。"""
    config_path = _write_legacy_config(
        tmp_path,
        f"""\
config_schema_version: {CURRENT_CONFIG_SCHEMA_VERSION}
model:
  preset_id: local-gemma-4-e4b-it
web:
  enabled: true
mcp:
# 是否启用 MCP client 启动与工具注册。
  enabled: false
# stdio MCP server 配置列表。
  servers:
    - server_id: minimax
      command: uvx
""",
    )

    cfg = load_config(config_path, load_env_file=False)

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "enabled" not in raw["mcp"]
    text = config_path.read_text(encoding="utf-8")
    assert "是否启用 MCP client 启动与工具注册。" not in text
    assert "stdio MCP server 配置列表。" in text
    assert cfg.mcp.servers[0].server_id == "minimax"


def test_migration_renames_legacy_web_origin_and_preserves_value(tmp_path: Path) -> None:
    """旧 origin 字段独立存在时，会搬值到 server_origin 再删除旧字段。"""
    legacy_key = "public_origin"
    legacy_origin = "http://192.168.31.23:8765"
    config_path = _write_legacy_config(
        tmp_path,
        f"""\
model:
  name: local
  base_url: http://127.0.0.1:1234/v1
web:
  {legacy_key}: {legacy_origin}
  enabled: true
""",
    )

    result = migrate_config_if_needed(config_path)
    cfg = load_config(config_path, load_env_file=False)

    assert result.migrated is True
    assert "web.server_origin" in result.added_fields
    assert f"web.{legacy_key}" in result.removed_fields
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["web"]["server_origin"] == legacy_origin
    assert legacy_key not in raw["web"]
    assert cfg.web.server_origin == legacy_origin


def test_migration_backfills_server_origin_for_current_yaml(tmp_path: Path) -> None:
    """当前版本 Web 配置缺少关键字段时会补入显式配置。"""
    config_path = _write_legacy_config(
        tmp_path,
        f"""\
config_schema_version: {CURRENT_CONFIG_SCHEMA_VERSION}
model:
  preset_id: local-gemma-4-e4b-it
web:
  enabled: true
  port: 60000
""",
    )

    result = migrate_config_if_needed(config_path)
    cfg = load_config(config_path, load_env_file=False)

    assert result.migrated is True
    assert "web.server_origin" in result.added_fields
    assert "mcp.servers" in result.added_fields
    assert "web_search.search_tool_name" in result.added_fields
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "server_origin" in raw["web"]
    assert "enabled" not in raw["mcp"]
    assert raw["mcp"]["servers"][0]["server_id"] == "minimax"
    assert raw["web_search"]["search_tool_name"] == "mcp__minimax__web_search"
    assert cfg.web.server_origin is None
    assert cfg.mcp.servers[0].server_id == "minimax"


def test_load_config_writes_editable_backfill_field_to_yaml(tmp_path: Path) -> None:
    """可编辑 backfill 字段不能只靠内存默认值，正式加载后必须写回 YAML。"""
    meta = get_field_meta("web.server_origin")
    assert meta is not None
    assert meta.editable is True
    config_path = _write_legacy_config(
        tmp_path,
        f"""\
config_schema_version: {CURRENT_CONFIG_SCHEMA_VERSION}
model:
  preset_id: local-gemma-4-e4b-it
web:
  enabled: true
  port: 60000
""",
    )

    cfg = load_config(config_path, load_env_file=False)

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "server_origin" in raw["web"]
    assert "mcp" in raw
    assert "web_search" in raw
    assert cfg.web.server_origin is None


def test_current_v06_rejects_static_model_fields(tmp_path: Path) -> None:
    """v0.6 严格拒绝已经搬入 catalog 的静态 model 字段。"""
    config_path = _write_legacy_config(
        tmp_path,
        f"""\
config_schema_version: {CURRENT_CONFIG_SCHEMA_VERSION}
model:
  preset_id: local-gemma-4-e4b-it
  name: local
  base_url: http://127.0.0.1:1234/v1
""",
    )

    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(config_path, load_env_file=False)

    assert "model.name" in str(exc_info.value)
    assert "model.base_url" in str(exc_info.value)


def test_current_v06_rejects_web_llm_presets(tmp_path: Path) -> None:
    """v0.6 严格拒绝 web.llm_presets，静态定义统一进入 catalog。"""
    config_path = _write_legacy_config(
        tmp_path,
        f"""\
config_schema_version: {CURRENT_CONFIG_SCHEMA_VERSION}
model:
  preset_id: local-gemma-4-e4b-it
web:
  enabled: true
  llm_presets:
    - id: bigmodel-glm5
      display_name: GLM Custom
      provider: openai_compatible
      base_url: https://proxy.example.test/v1
      model: glm-custom
      api_key_env: GLM_API_KEY
""",
    )

    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(config_path, load_env_file=False)

    assert "web.llm_presets" in str(exc_info.value)


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
