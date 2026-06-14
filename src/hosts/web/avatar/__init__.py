"""Avatar message registry 包公开入口。

本脚本只导出 AvatarManager、公开 DTO 和稳定错误类型。关键流程是
app.py 装配 Manager，Router 和测试从本包入口引用公开对象。关键边界是其它模块
注册 Avatar 消息时优先依赖 AvatarManager，避免跨模块引用内部 helper。
"""

from hosts.web.avatar.errors import AvatarMessageError
from hosts.web.avatar.manager import AvatarManager
from hosts.web.avatar.models import (
    AvatarAckBatchRequest,
    AvatarAckBatchResult,
    AvatarAckItemResult,
    AvatarAckRequest,
    AvatarAckStatus,
    AvatarCapabilities,
    AvatarChatRequest,
    AvatarMessageAction,
    AvatarMessageInput,
    AvatarMessageLevel,
    AvatarMessageListQuery,
    AvatarMessageListResult,
    AvatarMessageSnapshot,
    AvatarMessageStatus,
)

__all__ = [
    "AvatarAckBatchRequest",
    "AvatarAckBatchResult",
    "AvatarAckItemResult",
    "AvatarAckRequest",
    "AvatarAckStatus",
    "AvatarCapabilities",
    "AvatarChatRequest",
    "AvatarManager",
    "AvatarMessageAction",
    "AvatarMessageError",
    "AvatarMessageInput",
    "AvatarMessageLevel",
    "AvatarMessageListQuery",
    "AvatarMessageListResult",
    "AvatarMessageSnapshot",
    "AvatarMessageStatus",
]
