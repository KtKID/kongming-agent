"""统一配置加载。

:func:`load_config` 是唯一入口。读取顺序：

1. 若 ``path`` 参数显式传入，使用该路径。
2. 否则若环境变量 ``KONGMING_CONFIG`` 存在，使用它。
3. 否则使用仓库内的 ``config/default.yaml``（相对 :data:`_REPO_ROOT`）。

读完 YAML 之后，用 ``KONGMING_<SECTION>_<FIELD>``（全大写）格式的环境变量覆
盖单字段。例如：

- ``KONGMING_MODEL_API_KEY`` → ``model.api_key``
- ``KONGMING_MODEL_BASE_URL`` → ``model.base_url``
- ``KONGMING_RUNNER_MAX_TURNS`` → ``runner.max_turns``
- ``KONGMING_TOOL_SHELL_ENABLED`` → ``tool.shell.enabled``

覆盖规则只影响**标量字段**（str / int / float / bool），不允许通过环境变量
整体替换一个 section。多层嵌套（例如 ``tool.shell.enabled``）按下划线切成
一串 section path，从 YAML 产出的原始 dict 上按路径覆盖。

覆盖值类型解析规则：读出的环境变量都是字符串，交给 pydantic 在构造
:class:`Config` 时做强转（pydantic v2 对 ``"true"``/``"false"``/``"1"`` 等都
有内置解析）。这里不做提前 cast，避免在配置层出现第二套类型规则。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from config_loader.errors import ConfigLoadError, ConfigValidationError
from config_loader.models import Config

# 仓库根。config_loader/loader.py → config_loader/ → <repo root>。
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH: Path = _REPO_ROOT / "config" / "default.yaml"

# 环境变量前缀。任何以 ``KONGMING_`` 开头、且按 ``_`` 切分后能落到有效 section
# path 的变量都会参与覆盖。
_ENV_PREFIX = "KONGMING_"

# 已知的配置字段路径（tuple of section parts）。用于把 ``KONGMING_MODEL_API_KEY``
# 这种变量名匹配回 ``("model", "api_key")``。
#
# 这里不做反射扫描 pydantic 模型，而是显式列出——因为字段名里本身可能含下划线
# （如 ``api_key`` / ``base_url`` / ``max_turns``），反射解析歧义难排除。
_ENV_FIELD_PATHS: tuple[tuple[str, ...], ...] = (
    ("model", "provider"),
    ("model", "name"),
    ("model", "base_url"),
    ("model", "api_key"),
    ("model", "timeout"),
    ("model", "max_tokens"),
    ("model", "temperature"),
    ("runner", "max_turns"),
    ("session", "backend"),
    ("session", "store_path"),
    ("trace", "output_path"),
    ("logging", "level"),
    ("host", "kind"),
    ("approval", "mode"),
    ("tool", "shell", "enabled"),
    ("tool", "file", "enabled"),
)


def _resolve_config_path(explicit: str | Path | None) -> Path:
    """按优先级解析实际要读的配置文件路径。"""
    if explicit is not None:
        path = Path(explicit)
    else:
        env_path = os.environ.get("KONGMING_CONFIG")
        path = Path(env_path) if env_path else _DEFAULT_CONFIG_PATH
    return path


def _load_yaml(path: Path) -> dict[str, Any]:
    """读 YAML 并做基本结构校验。"""
    if not path.exists():
        raise ConfigLoadError(
            f"config file not found: {path}",
            details={"path": str(path)},
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigLoadError(
            f"failed to read config file {path}: {exc}",
            details={"path": str(path), "cause": type(exc).__name__},
        ) from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigLoadError(
            f"failed to parse YAML at {path}: {exc}",
            details={"path": str(path)},
        ) from exc

    # 允许空文件，按空 dict 处理（但 ``model`` 是必填，后面会在 pydantic 校验时报错）。
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigLoadError(
            f"config root must be a mapping, got {type(data).__name__} at {path}",
            details={"path": str(path)},
        )
    return data


def _env_var_name(path: tuple[str, ...]) -> str:
    """把 section path 翻译成环境变量名。"""
    return _ENV_PREFIX + "_".join(part.upper() for part in path)


def _set_nested(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """按 section path 写入嵌套 dict，缺失层级会逐级补 dict。"""
    cursor = data
    for key in path[:-1]:
        nxt = cursor.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[key] = nxt
        cursor = nxt
    cursor[path[-1]] = value


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """对已经从 YAML 读出来的 dict 按环境变量叠加覆盖。"""
    for field_path in _ENV_FIELD_PATHS:
        env_name = _env_var_name(field_path)
        if env_name in os.environ:
            _set_nested(data, field_path, os.environ[env_name])
    return data


def load_config(path: str | Path | None = None) -> Config:
    """加载并校验整体配置。

    Args:
        path: 显式配置文件路径；为 ``None`` 时走 ``KONGMING_CONFIG`` 环境变量，
            再 fallback 到仓库内 ``config/default.yaml``。

    Returns:
        校验通过的 :class:`Config` 实例。

    Raises:
        ConfigLoadError: 路径不存在、无法读取、YAML 解析失败。
        ConfigValidationError: 字段类型 / 约束 / 跨字段规则不满足。
    """
    resolved = _resolve_config_path(path)
    raw_data = _load_yaml(resolved)
    merged = _apply_env_overrides(raw_data)

    try:
        return Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigValidationError(
            f"config validation failed for {resolved}: {exc}",
            details={"path": str(resolved), "errors": exc.errors()},
        ) from exc


__all__ = ["load_config"]
