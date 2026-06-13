"""unit：WebFullLogConfig（full-log-v0.1）schema + env 覆盖 + 默认值。

覆盖点：

- 默认值（``enabled=False`` / ``path`` / ``queue_size=10000`` / ``include_http_body=False``）
- env ``KONGMING_WEB_FULL_LOG_*`` 覆盖单字段
- ``queue_size`` 的 ``ge=100`` 约束（校验失败抛 ConfigValidationError）
- yaml ``web.full_log`` section 覆盖
- ``frozen=True`` 不可变性

env 覆盖路径里 ``full_log`` 这种带下划线字段名通过显式 ``("web", "full_log", "enabled")``
tuple 表达，``_env_var_name`` 直接 ``"_".join(part.upper())`` 拼成
``KONGMING_WEB_FULL_LOG_ENABLED``，与 ``("web", "dev_mode")`` →
``KONGMING_WEB_DEV_MODE`` 走同一套机制，零歧义。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.config import load_config
from infrastructure.config.errors import ConfigValidationError
from infrastructure.config.models import WebFullLogConfig

# 已有 e2e 用 setting.yaml 作为基准；这里隔离用最小 yaml + tmp_path，避免本地
# setting.yaml 漂移污染断言。
_MINIMAL_YAML = """\
model:
  name: local-model
  base_url: http://127.0.0.1:1234/v1
  api_key: ""
"""


def _write_minimal_yaml(tmp_path: Path) -> Path:
    """写一份只含必填 model 段的最小 yaml，让 ``load_config`` 能装配出 Config。"""
    yaml_path = tmp_path / "setting.yaml"
    yaml_path.write_text(_MINIMAL_YAML, encoding="utf-8")
    return yaml_path


@pytest.mark.unit
def test_default_values(tmp_path: Path) -> None:
    """没有 yaml.web.full_log / 没有 env 时，全部走 WebFullLogConfig 默认值。"""
    yaml_path = _write_minimal_yaml(tmp_path)
    cfg = load_config(yaml_path, load_env_file=False)

    assert cfg.web.full_log.enabled is False
    assert cfg.web.full_log.path == ".kongming/logs/full_log.jsonl"
    assert cfg.web.full_log.rotate_daily is True
    assert cfg.web.full_log.include_http_body is False
    assert cfg.web.full_log.queue_size == 10000


@pytest.mark.unit
def test_env_override_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KONGMING_WEB_FULL_LOG_ENABLED=1`` → enabled True。"""
    yaml_path = _write_minimal_yaml(tmp_path)
    monkeypatch.setenv("KONGMING_WEB_FULL_LOG_ENABLED", "1")
    cfg = load_config(yaml_path, load_env_file=False)
    assert cfg.web.full_log.enabled is True


@pytest.mark.unit
def test_env_override_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KONGMING_WEB_FULL_LOG_PATH`` 替换路径默认值。"""
    yaml_path = _write_minimal_yaml(tmp_path)
    monkeypatch.setenv("KONGMING_WEB_FULL_LOG_PATH", "/tmp/custom.jsonl")
    cfg = load_config(yaml_path, load_env_file=False)
    assert cfg.web.full_log.path == "/tmp/custom.jsonl"


@pytest.mark.unit
def test_env_override_queue_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KONGMING_WEB_FULL_LOG_QUEUE_SIZE`` 接受字符串，pydantic 强转 int。"""
    yaml_path = _write_minimal_yaml(tmp_path)
    monkeypatch.setenv("KONGMING_WEB_FULL_LOG_QUEUE_SIZE", "500")
    cfg = load_config(yaml_path, load_env_file=False)
    assert cfg.web.full_log.queue_size == 500


@pytest.mark.unit
def test_queue_size_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``queue_size < 100`` 必须被 pydantic ``ge=100`` 约束拒掉。"""
    yaml_path = _write_minimal_yaml(tmp_path)
    monkeypatch.setenv("KONGMING_WEB_FULL_LOG_QUEUE_SIZE", "50")
    with pytest.raises(ConfigValidationError):
        load_config(yaml_path, load_env_file=False)


@pytest.mark.unit
def test_queue_size_above_maximum_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``queue_size > 1_000_000`` 必须被 pydantic ``le=1_000_000`` 约束拒掉。

    P1 #2：防误配把队列调成 10**10 这种巨大值导致 asyncio.Queue 占用无限内存
    （把"丢最旧"防爆设计变成 OOM）。
    """
    yaml_path = _write_minimal_yaml(tmp_path)
    monkeypatch.setenv("KONGMING_WEB_FULL_LOG_QUEUE_SIZE", "10000000")  # 10**7，超 1M
    with pytest.raises(ConfigValidationError):
        load_config(yaml_path, load_env_file=False)


@pytest.mark.unit
def test_queue_size_at_maximum_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``queue_size == 1_000_000`` 必须被接受（边界值含上限）。"""
    yaml_path = _write_minimal_yaml(tmp_path)
    monkeypatch.setenv("KONGMING_WEB_FULL_LOG_QUEUE_SIZE", "1000000")
    cfg = load_config(yaml_path, load_env_file=False)
    assert cfg.web.full_log.queue_size == 1_000_000


@pytest.mark.unit
def test_yaml_override(tmp_path: Path) -> None:
    """yaml.web.full_log 段写 ``enabled: true`` 可生效。"""
    yaml_path = tmp_path / "setting.yaml"
    yaml_path.write_text(
        _MINIMAL_YAML + "web:\n  full_log:\n    enabled: true\n    path: /var/log/kongming.jsonl\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path, load_env_file=False)
    assert cfg.web.full_log.enabled is True
    assert cfg.web.full_log.path == "/var/log/kongming.jsonl"


@pytest.mark.unit
def test_frozen_immutable() -> None:
    """WebFullLogConfig 设了 ``frozen=True``——实例后字段不可重新赋值。"""
    cfg = WebFullLogConfig()
    with pytest.raises((TypeError, ValueError)):
        # pydantic v2 frozen 模型赋值会抛 ValidationError (子类 ValueError)
        # 或 TypeError，两者都接受
        cfg.enabled = True  # type: ignore[misc]
