"""共享 SSE 行缓冲 + JSON 解包。

OpenAI Chat Completions 与 Anthropic Messages 两种流都走 Server-Sent Events
协议。此模块提供唯一的行缓冲 + JSON 解包函数 :func:`iter_sse_events`，
返回 ``(event_name, data)`` 二元组：

- OpenAI-compatible 流：SSE 只有 ``data:`` 行，``event_name`` 始终为 ``None``
- Anthropic Messages 流：每条记录先 ``event:`` 再 ``data:``，parser 需要用
  ``event_name`` 区分 content_block / message_delta 等生命周期事件

契约细节见 ``docs/llm-provider-v0.2/streaming/04-data-and-state.md`` §3.2。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def iter_sse_events(
    response: httpx.Response,
) -> AsyncIterator[tuple[str | None, dict[str, Any]]]:
    """从 httpx 流式 Response 中逐条 yield ``(event_name, data)`` 二元组。

    规则：

    - ``event:`` 行：记入"当前 event_name"状态，不 yield
    - ``data: [DONE]``：立即终止（``StopAsyncIteration``）
    - ``data: {...}``：yield ``(current_event_name, parsed_dict)``，然后清空
      event_name（SSE 规范：每条 message 的 event 字段在 data 发送后失效）
    - ``:...`` 注释行（心跳）/ ``retry:`` 行 / 空行：跳过
    - 处理跨 TCP 包的行拼接（buffer + ``b"\\n\\n"`` 分隔符）
    - JSON 解析失败的行：emit 一条 ``log.warning`` 后跳过，不抛异常
      （防御本地模型偶发脏数据）

    Args:
        response: 已用 ``httpx.AsyncClient.stream()`` 发起的流式响应对象。

    Yields:
        ``(event_name, data_dict)``。``event_name`` 为最近一条 ``event:`` 行的
        值（无则 ``None``）；``data_dict`` 是该 ``data:`` 行解析出的 JSON 对象。
    """

    buffer = b""
    current_event: str | None = None

    async for piece in response.aiter_bytes():
        if not piece:
            continue
        buffer += piece
        # SSE 规范用 \n\n 作为 message 分隔符；但为防御本地模型偶发只给 \n，
        # 我们按 "行" 逐行解析，靠"空行"触发当前 message 的消费节奏的同时，
        # 也在每条非空行上做即时处理（event: / data: / 注释 / retry）。
        while b"\n" in buffer:
            line_bytes, buffer = buffer.split(b"\n", 1)
            # 兼容 CRLF
            if line_bytes.endswith(b"\r"):
                line_bytes = line_bytes[:-1]

            if not line_bytes:
                # 空行：标志一条 SSE message 结束。此时 current_event 由下一
                # 条 data: 消耗；当前实现里 data: 行抵达时已立即 yield 并清空，
                # 因此这里无需额外动作。
                continue

            # 注释行（heartbeat 或 provider 自定义注解）
            if line_bytes.startswith(b":"):
                continue

            try:
                line = line_bytes.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("sse_reader: non-utf8 line dropped: %r", line_bytes[:80])
                continue

            if line.startswith("event:"):
                current_event = line[len("event:") :].strip() or None
                continue

            if line.startswith("retry:"):
                # retry 字段 V1 不消费（由 httpx / provider 自己的重试策略接管）
                continue

            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    # OpenAI-compatible 终止哨兵；立即结束迭代
                    return
                if not payload:
                    # 空 data：跳过
                    continue
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "sse_reader: failed to decode data line as JSON (%s): %r",
                        exc,
                        payload[:200],
                    )
                    # 继续处理后续行；清空 current_event 以贴合规范
                    current_event = None
                    continue
                if not isinstance(parsed, dict):
                    logger.warning(
                        "sse_reader: data line is not a JSON object: %r",
                        payload[:200],
                    )
                    current_event = None
                    continue
                yield (current_event, parsed)
                # SSE 规范：每条 message 的 event 字段在 data 发送后失效
                current_event = None
                continue

            # 其它字段（例如 id:）V1 忽略
            continue

    # 流结束后若 buffer 仍有未处理尾部数据（非以 \n 结尾）：尝试解析最后一行
    if buffer.strip():
        tail = buffer.strip()
        if tail.startswith(b"data:"):
            payload = tail[len(b"data:") :].strip().decode("utf-8", errors="replace")
            if payload and payload != "[DONE]":
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "sse_reader: failed to decode trailing data line as JSON (%s): %r",
                        exc,
                        payload[:200],
                    )
                else:
                    if isinstance(parsed, dict):
                        yield (current_event, parsed)


__all__ = ["iter_sse_events"]
