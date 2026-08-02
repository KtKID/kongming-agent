"""Web 宿主环境配置测试。"""

from __future__ import annotations

from pathlib import Path

from infrastructure.config import load_config


def _write_config(tmp_path: Path) -> Path:
    """写入包含最小模型配置的 YAML，输出配置文件路径。"""
    config_path = tmp_path / "setting.yaml"
    config_path.write_text(
        """
model:
  name: fake
  base_url: http://127.0.0.1:1234/v1
  api_key: ""
web:
  enabled: true
  host_environment: browser
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


def test_web_host_environment_env_override(tmp_path: Path, monkeypatch) -> None:
    """KONGMING_WEB_HOST_ENVIRONMENT 覆盖 YAML 中的 web.host_environment。"""
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("KONGMING_WEB_HOST_ENVIRONMENT", "xspace")

    cfg = load_config(config_path, load_env_file=False)

    assert cfg.web.host_environment == "xspace"
