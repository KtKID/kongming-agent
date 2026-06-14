"""Avatar 消息业务门户。

本脚本实现 AvatarManager，作为 Web host 内 Avatar message registry 的唯一
业务入口。关键流程是内部模块调用 register_message，Router 调用 list/ack/chat，
Manager 统一校验和委托 Repository/AssistantManager。关键类职责：维护跨模块
入口收口，避免外部模块直接依赖 Repository 或内部 helper。
"""

from __future__ import annotations

from datetime import datetime

from . import errors
from .assistant_manager import AvatarAssistantManager
from .models import (
    AvatarAckBatchRequest,
    AvatarAckBatchResult,
    AvatarAckItemResult,
    AvatarAckRequest,
    AvatarCapabilities,
    AvatarChatRequest,
    AvatarMessageInput,
    AvatarMessageListQuery,
    AvatarMessageListResult,
    AvatarMessageSnapshot,
)
from .repository import AvatarMessageRepository


class AvatarManager:
    """Avatar 消息业务 Manager。

    职责：收口消息注册、拉取、ack、capabilities 和 chat disabled 边界。
    关键输入：AvatarMessageRepository 和 AvatarAssistantManager。
    关键输出：Avatar 消息 DTO 或稳定业务错误。
    """

    def __init__(
        self,
        repository: AvatarMessageRepository,
        assistant: AvatarAssistantManager | None = None,
    ) -> None:
        """初始化 AvatarManager。

        关键输入：消息 Repository 和可选 AssistantManager。
        关键输出：可供 app.state 注入的业务门户实例。
        """
        self._repository = repository
        self._assistant = assistant or AvatarAssistantManager()

    @property
    def repository(self) -> AvatarMessageRepository:
        """返回底层 Repository，只供测试和装配诊断使用。"""
        return self._repository

    def register_message(
        self,
        message: AvatarMessageInput,
        *,
        now: datetime | None = None,
    ) -> AvatarMessageSnapshot:
        """注册 Avatar 消息。

        关键输入：AvatarMessageInput。
        关键输出：持久化后的消息快照。
        """
        return self._repository.register_message(message, now=now)

    def list_messages(
        self,
        query: AvatarMessageListQuery,
        *,
        now: datetime | None = None,
    ) -> AvatarMessageListResult:
        """查询 Avatar 消息。

        关键输入：AvatarMessageListQuery。
        关键输出：分页消息列表。
        """
        return self._repository.list_messages(query, now=now)

    def ack_message(
        self,
        message_id: str,
        request: AvatarAckRequest,
        *,
        now: datetime | None = None,
    ) -> AvatarMessageSnapshot:
        """确认或消费单条 Avatar 消息。

        关键输入：消息 ID 和 ack 请求。
        关键输出：幂等更新后的消息快照。
        """
        return self._repository.ack_message(message_id, request, now=now)

    def ack_messages(
        self,
        request: AvatarAckBatchRequest,
        *,
        now: datetime | None = None,
    ) -> AvatarAckBatchResult:
        """批量确认或消费 Avatar 消息。

        关键输入：批量 ack 请求。
        关键输出：逐条成功或错误结果。
        """
        results: list[AvatarAckItemResult] = []
        ack_request = AvatarAckRequest(
            status=request.status,
            consumer_id=request.consumer_id,
            at=request.at,
        )
        for message_id in request.message_ids:
            try:
                snapshot = self.ack_message(message_id, ack_request, now=now)
            except errors.AvatarMessageError as exc:
                results.append(
                    AvatarAckItemResult(
                        message_id=message_id,
                        ok=False,
                        error=exc.code,
                    )
                )
            else:
                results.append(
                    AvatarAckItemResult(
                        message_id=message_id,
                        ok=True,
                        message=snapshot,
                    )
                )
        return AvatarAckBatchResult(results=results)

    def capabilities(self) -> AvatarCapabilities:
        """返回 Avatar 能力声明。"""
        return self._assistant.capabilities()

    def chat(self, request: AvatarChatRequest) -> None:
        """处理 Avatar chat 请求。

        关键输入：AvatarChatRequest。
        关键输出：v1 固定返回 disabled 错误。
        """
        self._assistant.chat(request)


__all__ = ["AvatarManager"]
