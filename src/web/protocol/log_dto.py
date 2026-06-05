"""日志查看 REST DTO（full-log-v0.2）。

本文件定义日志尾部读取相关的 DTO，用于 ``GET /api/logs/{source}/tail`` 端面：

- :class:`LogSourceDTO`：日志 source 元数据（文件路径、格式、大小等）。
- :class:`LogLineDTO`：单行日志内容（原始文本 + 可选解析结果）。
- :class:`LogReadResponseDTO`：日志尾部读取响应（source + lines 分页）。

所有 DTO 继承 :class:`web.protocol._base._FrameBase`（``frozen=True``、
``extra='forbid'``），与其它 REST DTO 保持一致约束。
"""

from __future__ import annotations

from typing import Any, Literal

from web.protocol._base import _FrameBase

#: 日志文件格式枚举。
LogFormat = Literal["jsonl", "plain", "mixed"]


class LogSourceDTO(_FrameBase):
    """日志 source 元数据。"""

    type: str
    label: str
    format: LogFormat
    description: str
    path: str
    exists: bool
    size_bytes: int | None = None
    updated_at_ms: int | None = None


class LogLineDTO(_FrameBase):
    """单行日志内容。"""

    line_no: int | None = None
    raw: str
    parsed: dict[str, Any] | None = None
    parse_error: str | None = None


class LogReadResponseDTO(_FrameBase):
    """日志尾部读取响应。"""

    source: LogSourceDTO
    lines: list[LogLineDTO]
    truncated: bool
    read_bytes: int
    total_bytes: int | None = None


__all__: list[str] = [
    "LogFormat",
    "LogLineDTO",
    "LogReadResponseDTO",
    "LogSourceDTO",
]
