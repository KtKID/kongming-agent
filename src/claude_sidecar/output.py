"""sidecar → XSpace 事件输出层。

所有往 stdout 写事件都走 :class:`OutputWriter`：

- 自动注入 ``protocolVersion: '1'``
- 自动注入 ``ts``（ISO8601 UTC，调用方未指定时）
- 用 :class:`asyncio.Lock` 保护，防止多协程并发写交错
- ``ensure_ascii=False`` — 中文 message 不转义

stderr 不在本模块管辖（freeform 调试日志，由调用方自己写）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

PROTOCOL_VERSION = "1"


def _utc_iso_now() -> str:
    """生成符合契约 §7.1 的 ISO8601 UTC 时间戳。

    输出形如 ``2026-05-01T18:00:00.123456+00:00``——保留微秒，
    时区显式标注为 ``+00:00``，避免不同语言环境对 ``Z`` 后缀的解析分歧。
    """
    return datetime.now(UTC).isoformat()


class OutputWriter:
    """JSONL 事件序列化器，自动注入 protocolVersion / ts，并发安全。"""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._lock = asyncio.Lock()

    async def emit(self, event: dict[str, Any]) -> None:
        """写一条事件。

        强制注入 ``protocolVersion: '1'``——即使调用方传了别的值也覆盖
        （避免 v2 协议尚未就位时上游误传）。
        ``ts`` 字段缺失时自动填当前 UTC 时间，已有则尊重调用方。
        """
        payload: dict[str, Any] = dict(event)
        payload["protocolVersion"] = PROTOCOL_VERSION
        payload.setdefault("ts", _utc_iso_now())
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        async with self._lock:
            self._stream.write(line)
            self._stream.flush()

    async def emit_ready(self) -> None:
        """sidecar 进程级别的"我准备好了"事件。

        契约 §7.2 ``claude_sidecar_ready`` 不带 threadId / runId。
        """
        await self.emit({"type": "claude_sidecar_ready"})

    async def emit_transport_error(
        self,
        message: str,
        *,
        recoverable: bool,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """协议级错误事件（契约 §7.16）。

        ``thread_id`` / ``run_id`` 缺省时**不写字段**（不是 ``"runId": null``），
        因为契约说"sidecar 启动期错误允许没有 threadId / runId"——不写字段
        比写 null 更符合"没有"语义，下游 JSON Schema 验证更干净。
        """
        event: dict[str, Any] = {
            "type": "claude_transport_error",
            "message": message,
            "recoverable": recoverable,
        }
        if thread_id is not None:
            event["threadId"] = thread_id
        if run_id is not None:
            event["runId"] = run_id
        await self.emit(event)
