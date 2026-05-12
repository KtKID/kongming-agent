"""Evolution 独立日志配置——全量打印到独立文件 + 结构化上下文。

最佳实践：
- evolution.* 所有子 logger 统一路由到独立 FileHandler
- 文件级 DEBUG（全量），不受全局 logging.level 限制
- RotatingFileHandler 防磁盘爆（10MB × 3 轮转）
- 格式含 timestamp + level + module + message，方便 grep
- 调用方只需 ``setup_evolution_logging(root_dir)``，模块级 ``getLogger(__name__)`` 自动生效
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = ["setup_evolution_logging"]

_LOGGER_NAME = "evolution"
_LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 3
_DEFAULT_LOG_DIR = "logs"
_DEFAULT_LOG_FILE = "evolution.log"

_configured = False


def setup_evolution_logging(
    kongming_home: Path,
    *,
    file_level: int = logging.DEBUG,
    console_level: int | None = None,
) -> Path:
    """为 evolution.* logger 层级配置独立 FileHandler。

    Args:
        kongming_home: ``.kongming/`` 根目录。日志写到
            ``<kongming_home>/logs/evolution.log``。
        file_level: 文件日志级别，默认 DEBUG（全量）。
        console_level: 可选额外 console handler 级别。None 不加
            （全局 root logger 已有 console handler，evolution 日志会自然冒泡到那里）。

    Returns:
        日志文件路径。

    幂等：重复调用不会重复添加 handler。
    """
    global _configured
    if _configured:
        log_dir = kongming_home / _DEFAULT_LOG_DIR
        return log_dir / _DEFAULT_LOG_FILE

    log_dir = kongming_home / _DEFAULT_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / _DEFAULT_LOG_FILE

    evo_logger = logging.getLogger(_LOGGER_NAME)
    evo_logger.setLevel(logging.DEBUG)  # 允许全量通过，handler 各自过滤
    evo_logger.propagate = True  # 仍冒泡到 root（console 输出由全局控制）

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)

    # 独立文件 handler：全量 DEBUG，RotatingFileHandler 防爆
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    evo_logger.addHandler(file_handler)

    # 可选独立 console handler（一般不需要——全局 root logger 已有）
    if console_level is not None:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(console_level)
        console_handler.setFormatter(formatter)
        evo_logger.addHandler(console_handler)

    _configured = True
    evo_logger.info(
        "evolution logging initialized: %s (level=%s)", log_path, logging.getLevelName(file_level)
    )
    return log_path
