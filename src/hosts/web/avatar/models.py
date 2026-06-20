"""Avatar 消息注册模块公开数据模型。

本脚本定义 Avatar 消息、查询、ack、capability 和 chat 的内部 DTO。
关键流程是 Router 把 HTTP wire DTO 转换为这些模型，AvatarManager 调用
Repository 持久化和查询，再由 Router 转回 camelCase wire 响应。
关键类职责：枚举固定状态取值，Input/Query/Result 类承载 Manager 和
Repository 之间的稳定合同。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hosts.web.protocol.rest_models import UserInputAttachment


class AvatarMessageLevel(StrEnum):
    """Avatar 消息严重程度。

    关键输入：内部模块或 HTTP 调试注册传入的 level 字符串。
    关键输出：Repository 可持久化、XSpace 可过滤的稳定 level。
    """

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class AvatarMessageStatus(StrEnum):
    """Avatar 消息生命周期状态。

    关键输入：Repository 中的状态字符串或 ack 请求。
    关键输出：Manager 和 Router 使用的状态枚举。
    """

    ACTIVE = "active"
    ACKED = "acked"
    CONSUMED = "consumed"
    EXPIRED = "expired"


class AvatarAckStatus(StrEnum):
    """Avatar ack 目标状态。

    关键输入：XSpace ack 请求中的 status。
    关键输出：Repository 要推进到的确认状态。
    """

    ACKED = "acked"
    CONSUMED = "consumed"


class AvatarMessageAction(BaseModel):
    """Avatar 消息建议动作。

    职责：提供事实层动作建议，XSpace 自行决定展示和点击行为。
    关键输入：动作类型、可选标签、目标标识和扩展 payload。
    关键输出：可 JSON 序列化的 action DTO。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["open_thread", "open_approval", "open_url", "none"] = "none"
    label: str | None = Field(default=None, max_length=120)
    target: str | None = Field(default=None, max_length=1000)
    payload: dict[str, Any] = Field(default_factory=dict)


class AvatarMessageInput(BaseModel):
    """注册 Avatar 消息的输入 DTO。

    职责：承载 Kongming 内部模块注册给 Avatar 的事实消息。
    关键输入：来源、标题、正文、关联 thread/run/request、动作和 dedupe key。
    关键输出：Manager 可校验并交给 Repository 持久化的输入模型。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=2000)
    level: AvatarMessageLevel = AvatarMessageLevel.INFO
    priority: int = Field(default=50, ge=0, le=100)
    thread_id: str | None = Field(default=None, max_length=256)
    run_id: str | None = Field(default=None, max_length=256)
    request_id: str | None = Field(default=None, max_length=256)
    action: AvatarMessageAction | None = None
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=512)
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AvatarMessageSnapshot(BaseModel):
    """Avatar 消息快照。

    职责：表示 Repository 中一条消息的完整当前状态。
    关键输入：持久化记录字段。
    关键输出：Manager 和 Router 返回给调用方的消息快照。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str
    sequence: int
    revision: int
    status: AvatarMessageStatus
    input: AvatarMessageInput
    created_at: datetime
    updated_at: datetime
    acked_at: datetime | None = None
    consumed_at: datetime | None = None


class AvatarMessageListQuery(BaseModel):
    """Avatar 消息查询条件。

    职责：承载 REST list 和内部查询的过滤条件。
    关键输入：cursor、limit、since、level、source、thread_id、status。
    关键输出：Repository 可直接消费的查询模型。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)
    since: datetime | None = None
    level: list[AvatarMessageLevel] | None = None
    source: list[str] | None = None
    thread_id: str | None = None
    status: list[AvatarMessageStatus] | None = None


class AvatarMessageListResult(BaseModel):
    """Avatar 消息查询结果。

    职责：返回分页消息、下一页 cursor 和服务端时间。
    关键输入：Repository 查询结果。
    关键输出：Router 可序列化的列表结果。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[AvatarMessageSnapshot]
    next_cursor: str | None
    server_time: datetime


class AvatarAckRequest(BaseModel):
    """Avatar ack 请求。

    职责：描述 XSpace 对单条消息的确认或消费意图。
    关键输入：目标状态和消费方标识。
    关键输出：Manager/Repository 可执行的 ack 请求。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: AvatarAckStatus = AvatarAckStatus.CONSUMED
    consumer_id: str = Field(default="xspace-avatar", min_length=1, max_length=160)
    at: datetime | None = None


class AvatarAckBatchRequest(BaseModel):
    """Avatar 批量 ack 请求。

    职责：承载多条 message id 的同一 ack 动作。
    关键输入：message_ids 和 ack 请求参数。
    关键输出：Manager 可批量执行的 ack 输入。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_ids: list[str] = Field(min_length=1, max_length=100)
    status: AvatarAckStatus = AvatarAckStatus.CONSUMED
    consumer_id: str = Field(default="xspace-avatar", min_length=1, max_length=160)
    at: datetime | None = None


class AvatarAckItemResult(BaseModel):
    """Avatar 批量 ack 单项结果。

    职责：表示批量 ack 中某条消息的成功快照或稳定错误。
    关键输入：Repository ack 结果或捕获的业务错误。
    关键输出：Router 批量 ack 响应项。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str
    ok: bool
    message: AvatarMessageSnapshot | None = None
    error: str | None = None


class AvatarAckBatchResult(BaseModel):
    """Avatar 批量 ack 结果。

    职责：承载批量 ack 的所有单项处理结果。
    关键输入：Manager 批量处理结果。
    关键输出：Router 可序列化的响应体。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    results: list[AvatarAckItemResult]


class AvatarCapabilities(BaseModel):
    """Avatar 能力声明。

    职责：告诉 XSpace 当前 Kongming 侧支持哪些 Avatar 后端能力。
    关键输入：Manager 和 AssistantManager 当前配置。
    关键输出：capabilities REST 响应。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol_version: Literal["1"] = "1"
    message_registry: bool = True
    rest_list: bool = True
    rest_ack: bool = True
    ws_notifications: bool = False
    avatar_chat: bool = True
    avatar_realtime_chat: bool = True
    chat_transports: dict[str, str] = Field(
        default_factory=lambda: {
            "websocket": "/ws/avatar/v1/threads/{threadId}",
            "rest": "/api/avatar/v1/chat",
        }
    )
    required_scopes: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "list": ["avatar.read", "thread.read"],
            "ack": ["avatar.ack"],
            "chat": ["avatar.chat"],
        }
    )


class AvatarChatRequest(BaseModel):
    """Avatar 反向对话请求。

    职责：承载 XSpace Avatar 通过 REST 进入 generic_chat thread 的输入合同。
    关键输入：文本、可选 thread、preset/cwd、reasoning effort、附件和客户端能力。
    关键输出：AssistantManager 可处理的 chat 请求。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=8000)
    thread_id: str | None = Field(default=None, max_length=256)
    preset_id: str | None = Field(default=None, max_length=256)
    cwd: str = ""
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    attachments: list[UserInputAttachment] | None = None
    client_message_id: str | None = Field(default=None, max_length=256)
    device_id: str | None = Field(default=None, max_length=256)
    capabilities: dict[str, bool] = Field(default_factory=dict)


class AvatarChatAccepted(BaseModel):
    """Avatar chat accepted 响应。

    职责：表示 REST 消息已进入普通 generic_chat run 队列。
    关键输入：AssistantManager 创建或校验后的 thread/run/transport 信息。
    关键输出：Router 可转成 XSpace wire DTO 的 accepted 响应。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool = True
    thread_id: str
    run_id: str
    transport: Literal["websocket", "rest"] = "websocket"
    websocket_url: str
    server_time: datetime


__all__ = [
    "AvatarAckBatchRequest",
    "AvatarAckBatchResult",
    "AvatarAckItemResult",
    "AvatarAckRequest",
    "AvatarAckStatus",
    "AvatarCapabilities",
    "AvatarChatAccepted",
    "AvatarChatRequest",
    "AvatarMessageAction",
    "AvatarMessageInput",
    "AvatarMessageLevel",
    "AvatarMessageListQuery",
    "AvatarMessageListResult",
    "AvatarMessageSnapshot",
    "AvatarMessageStatus",
]
