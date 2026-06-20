"""Kongming 运行数据路径入口。

本模块定义两类业务根：
- ``kongming_home``：用户级 ``.kongming`` 运行数据根。
- ``thread.cwd``：每个 thread 归属的工作路径，由 thread metadata 或宿主传入。

``get_kongming_home`` 是 ``.kongming`` 运行数据根的唯一入口。
``resolve_kongming_path`` 负责把配置中的 ``.kongming/*`` 派生到
``kongming_home``，避免运行数据路径继续按进程 cwd 分叉。
``find_existing_kongming_home_config`` 负责统一查找用户级 setting.yaml。
"""

from __future__ import annotations

import os
from pathlib import Path

_KONGMING_DIRNAME = ".kongming"


def get_kongming_home() -> Path:
    """返回 ``.kongming`` 运行数据根的绝对路径。

    优先级：
    1. ``KONGMING_HOME``，支持绝对路径、相对路径和 ``~``。
    2. ``Path.home() / ".kongming"``。

    本函数只返回路径对象，调用方按需创建目录。
    """
    env_val = os.environ.get("KONGMING_HOME")
    if env_val and env_val.strip():
        return Path(env_val).expanduser().resolve()
    return (Path.home() / _KONGMING_DIRNAME).resolve()


def default_kongming_home_config_path(kongming_home: Path | None = None) -> Path:
    """返回用户级主配置默认写入路径。

    当前用户级可写配置使用 ``<kongming_home>/setting.yaml``。历史上部分 Web
    控制入口还读过 ``<kongming_home>/config/setting.yaml``，读取时继续兼容，
    新建和 XSpace seed 统一落到根部 ``setting.yaml``。
    """
    home = kongming_home.resolve() if kongming_home is not None else get_kongming_home()
    return home / "setting.yaml"


def kongming_home_config_candidates(kongming_home: Path | None = None) -> tuple[Path, ...]:
    """返回用户级 setting.yaml 的兼容候选路径。

    顺序代表优先级：
    1. ``<kongming_home>/setting.yaml``：当前用户级主配置路径；
    2. ``<kongming_home>/config/setting.yaml``：早期 Web 控制入口兼容路径。
    """
    home = kongming_home.resolve() if kongming_home is not None else get_kongming_home()
    return (
        home / "setting.yaml",
        home / "config" / "setting.yaml",
    )


def find_existing_kongming_home_config(kongming_home: Path | None = None) -> Path | None:
    """查找已存在的用户级配置文件。

    Args:
        kongming_home: 显式运行时 home；为空时使用 :func:`get_kongming_home`。

    Returns:
        找到时返回配置路径；没有用户级配置时返回 ``None``。
    """
    for candidate in kongming_home_config_candidates(kongming_home):
        if candidate.exists():
            return candidate
    return None


def resolve_kongming_path(path: str | Path, *, kongming_home: Path | None = None) -> Path:
    """解析 Kongming 配置路径。

    规则：
    - 绝对路径和 ``~`` 路径直接 ``expanduser().resolve()``。
    - 字面以 ``.kongming`` 开头的相对路径派生到 ``kongming_home`` 或
      ``get_kongming_home()``。
    - 其他相对路径按 Python 当前进程路径规则解析为绝对路径。

    运行数据配置字段应通过本函数消费，确保 ``.kongming/*`` 的默认落点统一。
    """
    expanded = Path(path).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()

    parts = expanded.parts
    if parts and parts[0] == _KONGMING_DIRNAME:
        suffix = Path(*parts[1:]) if len(parts) > 1 else Path()
        home = kongming_home.resolve() if kongming_home is not None else get_kongming_home()
        return (home / suffix).resolve()

    return expanded.resolve()


__all__ = [
    "default_kongming_home_config_path",
    "find_existing_kongming_home_config",
    "get_kongming_home",
    "kongming_home_config_candidates",
    "resolve_kongming_path",
]
