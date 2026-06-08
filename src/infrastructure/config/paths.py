"""统一 .kongming 根目录入口。

:func:`get_kongming_home` 是 kongming-agent 访问 ``.kongming/`` 目录的唯一权威
入口。本次（v0.1.3 prompt-modules）只在 prompts 装配里使用；memory / trace /
sessions 等现存模块的 ``.kongming/xxx`` 路径拼接不在本次范围内（另立 task
统一收口）。

**优先级**：

1. 环境变量 ``KONGMING_HOME``（支持绝对 / 相对路径，``~`` 会被展开）
2. ``Path.cwd() / ".kongming"``

本函数不创建目录，只返回 :class:`Path` 对象；所有消费者按需自行 ``mkdir``。

注意：``KONGMING_HOME`` 不是 :class:`infrastructure.config.models.Config` 字段，而是
和 ``KONGMING_CONFIG`` 同类的"特殊入口 env"，因此它不走 loader.py 的
``_ENV_FIELD_PATHS`` 覆盖机制。
"""

from __future__ import annotations

import os
from pathlib import Path


def get_kongming_home() -> Path:
    """返回 ``.kongming`` 根目录的绝对路径。

    Returns:
        已调用 :meth:`Path.expanduser` 和 :meth:`Path.resolve` 的绝对路径。

    行为说明：
        - 若 ``KONGMING_HOME`` 环境变量存在且 **strip 后非空**，以其为准；
          空字符串 ``""`` 视作"未设置"，回退到 cwd 分支（刻意的防御，避免
          用户误把 env 置空导致 ``Path("")`` 落到当前目录）。
        - 否则返回 ``Path.cwd() / ".kongming"``。
        - 不保证目录存在；调用方按需 :meth:`Path.mkdir`。
    """
    env_val = os.environ.get("KONGMING_HOME")
    if env_val and env_val.strip():
        return Path(env_val).expanduser().resolve()
    return (Path.cwd() / ".kongming").resolve()


__all__ = ["get_kongming_home"]
