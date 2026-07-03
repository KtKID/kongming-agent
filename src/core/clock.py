"""统一时间工具：tz-aware 单一时间来源。

取代散落各处的 ``int(time.time()*1000)`` 和 ``_now_iso`` / ``_utc_now_iso`` 等
local helper。全库 epoch-ms / ISO 时间戳只应通过本模块的 :func:`now_epoch_ms`
和 :func:`now_iso` 获取，保证口径统一、便于排障。

设计要点：
- 全部基于标准库 :class:`datetime.datetime`，tz-aware UTC（``datetime.now(UTC)``）。
- 与 naive ``time.time()`` 的绝对值相同（均为 Unix epoch 秒），无行为差异，
  仅来源单一化、并显式带时区。
- 纯函数、无状态、并发安全（无共享可变状态）。
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_epoch_ms() -> int:
    """tz-aware → epoch 毫秒。

    取代散落各处的 ``int(time.time()*1000)``。每次调用返回当前时刻的
    epoch 毫秒数（int），单调递增、并发安全。

    Returns:
        当前 UTC 时刻的 epoch 毫秒整数。
    """
    return int(datetime.now(UTC).timestamp() * 1000)


def now_iso() -> str:
    """tz-aware ISO8601 字符串。

    取代散落各处的 ``_now_iso`` / ``_utc_now_iso`` helper。返回带 ``+00:00``
    时区后缀的 ISO8601 字符串。

    Returns:
        当前 UTC 时刻的 ISO8601 字符串（tz-aware）。
    """
    return datetime.now(UTC).isoformat()


__all__ = ["now_epoch_ms", "now_iso"]
