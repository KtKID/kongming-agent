"""Steer 链路统一数据载体。

``SteerRequest`` 是"run 进行中追加补充输入"这条链路的唯一数据结构真源，
贯穿 Web → HostDispatcher → SessionEngine → Runner 全链路。此前该链路只传裸
``str``，导致 Runner emit 的 ``steer.injected`` 事件只能带 ``content_length``，
消费端被迫用正文长度匹配队列项身份——同长度并发、编辑后长度变化、事件乱序时会错账。
改用 ``SteerRequest`` 后，``pending_input_id`` 成为消账主键，未来扩展字段只改本
dataclass，下游签名不动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SteerRequest:
    """一次 steer（补充输入注入）请求。

    Attributes:
        text: 注入 session 历史的内容真源。Runner drain 时 ``Message.user(text)``
            落进历史，下一次 LLM 请求可见。
        pending_input_id: 队列项稳定身份（典型值 ``pin-<hex>``）。Web 消费用它精确
            匹配 send-now claim，``steer.injected`` 事件原样回带。``None`` 表示调用
            方未提供身份（如外部直调），此时 Web 侧记 error 不消账，避免盲弹错账。
        metadata: 预留扩展位（reasoning_effort 等），当前只透传不改。
    """

    text: str
    pending_input_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
