"""Conversation reference 协议 DTO。

本模块定义 Web slash catalog 选择项进入对话上下文时的共享协议形态。
核心流程：catalog provider 生成 ``ConversationReferenceTemplate``，前端在
Composer 中物化为 ``ConversationReferenceDTO``，随 ``user.input`` 帧提交，
后端写入 ``Message.metadata["conversation_references"]`` 并由 prompt assembly
解析。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from hosts.web.protocol._base import _FrameBase

ConversationReferenceKind = Literal["skill", "command", "workflow_strategy", "workflow_run"]
ConversationReferenceActivation = Literal[
    "inject_context",
    "execute_command",
    "start_workflow",
    "guide_payload",
    "open_viewer",
]


class ConversationReferenceTemplate(_FrameBase):
    """Catalog item 输出的引用模板。"""

    kind: ConversationReferenceKind
    ref: str
    label: str
    activation: ConversationReferenceActivation
    source_ref: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationReferenceDTO(ConversationReferenceTemplate):
    """用户消息提交时携带的结构化引用。"""

    id: str


__all__ = [
    "ConversationReferenceActivation",
    "ConversationReferenceDTO",
    "ConversationReferenceKind",
    "ConversationReferenceTemplate",
]
