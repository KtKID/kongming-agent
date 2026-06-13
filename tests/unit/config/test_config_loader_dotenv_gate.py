"""配置加载器的 dotenv 隔离测试。

覆盖 ``KONGMING_SKIP_DOTENV`` 对本地 ``.env`` 的硬隔离，确保 push gate
即使在仓库根目录运行，也不会把开发机真实模型配置带进单测。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.config import load_config


@pytest.mark.unit
def test_kongming_skip_dotenv_ignores_project_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """设置 KONGMING_SKIP_DOTENV 后，配置加载只使用 yaml 和显式环境变量。"""

    config_path = tmp_path / "setting.yaml"
    config_path.write_text(
        "model:\n  name: from-yaml\n  base_url: http://127.0.0.1:1234/v1\n  api_key: ''\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("KONGMING_MODEL_NAME=from-dotenv\n", encoding="utf-8")

    monkeypatch.setenv("KONGMING_SKIP_DOTENV", "1")
    monkeypatch.delenv("KONGMING_MODEL_NAME", raising=False)
    cfg = load_config(config_path)

    assert cfg.model.name == "from-yaml"
