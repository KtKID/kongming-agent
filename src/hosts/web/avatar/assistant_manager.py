"""Avatar 反向对话能力门户。

本脚本实现 v1 AvatarAssistantManager。当前阶段只提供 capabilities 和
稳定 disabled chat 语义，不连接真实模型或外部 API。关键流程是
AvatarManager 聚合 capabilities，Router chat endpoint 调用本 Manager 后得到
avatar_capability_disabled。关键类职责：为后续独立 Avatar LLM 通道保留入口。
"""

from __future__ import annotations

from . import errors
from .models import AvatarCapabilities, AvatarChatRequest


class AvatarAssistantManager:
    """Avatar 反向对话 Manager。

    职责：提供 AvatarChat 能力声明和 v1 disabled chat 响应。
    关键输入：AvatarChatRequest。
    关键输出：AvatarCapabilities 或稳定 disabled 错误。
    """

    def capabilities(self) -> AvatarCapabilities:
        """返回 v1 Avatar capabilities。

        关键输入：无。
        关键输出：avatar_chat 固定为 False 的能力声明。
        """
        return AvatarCapabilities()

    def chat(self, request: AvatarChatRequest) -> None:
        """处理 Avatar chat 请求。

        关键输入：AvatarChatRequest。
        关键输出：v1 固定抛出 avatar_capability_disabled。
        """
        _ = request
        raise errors.capability_disabled("avatar_chat")


__all__ = ["AvatarAssistantManager"]
