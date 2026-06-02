"""devtools —— 开发期可观测性工具包。

本包目前只包含 ``FullLogger``，用于把前后端通信（WS C2S/S2C 帧 + REST 元数据）
以"组装后最终格式"全量写盘到 ``.kongming/logs/full_log.jsonl``（跟 ``trace.jsonl``
同 ``.kongming/`` home 体系），便于离线分析、bug 回溯。
**默认关闭**，通过 env ``KONGMING_FULL_LOG=1`` 或 ``cfg.web.full_log.enabled=true`` 启用。

设计真源：``docs/full-log-v0.1/README.md``。
"""

from __future__ import annotations

from .full_logger import FullLogger, get_full_logger, init_full_logger

__all__ = ["FullLogger", "get_full_logger", "init_full_logger"]
