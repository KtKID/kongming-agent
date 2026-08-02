"""模型配置 v0.6 双文件迁移合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from infrastructure.config import migrations
from infrastructure.config.migrations import migrate_config_if_needed

_GLM_V05 = """\
config_schema_version: v0.5
model:
  provider: openai_compatible
  name: glm-5.2
  base_url: https://open.bigmodel.cn/api/coding/paas/v4
  api_key: ""
  api_key_header: authorization-bearer
  timeout: 60
  max_tokens: 65536
  temperature: 1.0
  reasoning_effort: high
  reasoning_profiles: {}
web:
  llm_presets: []
"""


_CUSTOM_V05 = """\
config_schema_version: v0.5
model:
  provider: openai_compatible
  name: custom-local
  base_url: http://127.0.0.1:7777/v1
  api_key: ""
  api_key_header: authorization-bearer
  timeout: 42
  max_tokens: 8192
  temperature: 0.2
  reasoning_effort:
  reasoning_profiles: {}
web:
  llm_presets: []
"""


def _write(tmp_path: Path, text: str) -> Path:
    """写入旧 setting。"""
    path = tmp_path / "setting.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_v05_builtin_model_migrates_to_minimal_selection(tmp_path: Path) -> None:
    """可精确匹配内置 catalog 的 GLM 只保留 preset 与 effort。"""
    setting = _write(tmp_path, _GLM_V05)

    result = migrate_config_if_needed(setting)
    raw = yaml.safe_load(setting.read_text(encoding="utf-8"))

    assert result.source_version == "v0.5"
    assert result.target_version == "v0.6"
    assert raw["model"] == {
        "preset_id": "bigmodel-glm5-1m",
        "reasoning_effort": "high",
    }
    assert "llm_presets" not in raw.get("web", {})
    assert not (tmp_path / "model-providers.yaml").exists()


def test_v05_custom_local_model_moves_to_user_catalog(tmp_path: Path) -> None:
    """未匹配的本地模型生成稳定 user catalog 定义。"""
    setting = _write(tmp_path, _CUSTOM_V05)

    migrate_config_if_needed(setting)
    raw_setting = yaml.safe_load(setting.read_text(encoding="utf-8"))
    catalog_path = tmp_path / "model-providers.yaml"
    raw_catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))

    preset_id = raw_setting["model"]["preset_id"]
    model = raw_catalog["providers"][0]["models"][0]
    assert raw_catalog["version"] == 2
    assert model["preset_id"] == preset_id
    assert model["model"] == "custom-local"
    assert raw_catalog["providers"][0]["default_base_url"] == "http://127.0.0.1:7777/v1"
    assert raw_catalog["providers"][0]["request_defaults"] == {
        "timeout_seconds": 42,
        "max_tokens": 8192,
        "temperature": 0.2,
    }
    assert "api_key" not in raw_catalog["providers"][0]
    assert "super-secret" not in catalog_path.read_text(encoding="utf-8")


def test_two_file_failure_restores_setting_and_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二次 replace 失败时恢复两份原始文件并清理 marker。"""
    setting = _write(tmp_path, _CUSTOM_V05)
    catalog = tmp_path / "model-providers.yaml"
    catalog.write_text("version: 2\nproviders: []\n", encoding="utf-8")
    original_setting = setting.read_bytes()
    original_catalog = catalog.read_bytes()
    real_replace = migrations._replace_prepared_file
    calls = 0

    def fail_second(source: Path, target: Path) -> None:
        """第一次替换成功，第二次模拟进程级写入失败。"""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replace failure")
        real_replace(source, target)

    monkeypatch.setattr(migrations, "_replace_prepared_file", fail_second)

    with pytest.raises(Exception):
        migrate_config_if_needed(setting)

    assert setting.read_bytes() == original_setting
    assert catalog.read_bytes() == original_catalog
    assert not (tmp_path / ".model-config-v06-transaction.json").exists()


def test_second_v06_migration_performs_zero_writes(tmp_path: Path) -> None:
    """完成迁移后再次执行保持 setting/catalog 内容与 mtime。"""
    setting = _write(tmp_path, _CUSTOM_V05)
    first = migrate_config_if_needed(setting)
    catalog = tmp_path / "model-providers.yaml"
    before = (setting.read_bytes(), catalog.read_bytes())
    mtimes = (setting.stat().st_mtime_ns, catalog.stat().st_mtime_ns)

    second = migrate_config_if_needed(setting)

    assert first.migrated is True
    assert second.migrated is False
    assert (setting.read_bytes(), catalog.read_bytes()) == before
    assert (setting.stat().st_mtime_ns, catalog.stat().st_mtime_ns) == mtimes
