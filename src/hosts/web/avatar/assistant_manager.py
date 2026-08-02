"""Avatar 对话能力门户。

本脚本实现 AvatarAssistantManager，把 XSpace Avatar chat 请求接入现有
generic_chat thread 运行链路。关键流程是校验或创建 thread，复用
ThreadManager 的 submit_avatar_input 入口启动普通对话，再返回 REST
accepted 响应。关键函数职责：capabilities 输出能力声明，chat 处理 REST 首发
和已有 thread 消息，approval 由全局 ApprovalManager 与 Avatar approval API 处理。
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from . import errors
from .models import AvatarCapabilities, AvatarChatAccepted, AvatarChatRequest

logger = logging.getLogger(__name__)


class AvatarAssistantManager:
    """Avatar 对话 Manager。

    职责：提供 AvatarChat 能力声明，并把 Avatar 输入提交到 generic_chat
    ThreadManager 链路。
    关键输入：ThreadManagerProtocol 兼容对象和 AvatarChatRequest。
    关键输出：AvatarCapabilities 或 AvatarChatAccepted。
    """

    def __init__(self, thread_manager: Any | None = None) -> None:
        """初始化 AvatarAssistantManager。

        关键输入：可选 ThreadManagerProtocol；测试可传 fake manager。
        关键输出：可由 AvatarManager 聚合的对话门户。
        """
        self._thread_manager = thread_manager

    def capabilities(self) -> AvatarCapabilities:
        """返回 Avatar capabilities。

        关键输入：当前 Manager 配置。
        关键输出：声明支持 REST chat 与实时 WebSocket chat。
        """
        return AvatarCapabilities()

    async def chat(self, request: AvatarChatRequest) -> AvatarChatAccepted:
        """处理 Avatar REST chat 请求。

        关键输入：AvatarChatRequest，thread_id 可空。
        关键输出：accepted 响应；后台 task 负责真实 generic_chat run。
        """
        tm = self._require_thread_manager()
        metadata = await self._resolve_thread(tm, request)
        thread_id = str(getattr(metadata, "id", request.thread_id or ""))

        await self._ensure_runtime_current(tm, thread_id)
        attachments = self._attachments_to_dicts(request)
        run_id = self._new_run_id(thread_id)
        try:
            await tm.submit_avatar_input(
                thread_id,
                request.text,
                request_id=request.client_message_id,
                reasoning_effort=request.reasoning_effort,
                attachments=attachments,
                avatar_run_id=run_id,
            )
        except KeyError as exc:
            raise errors.thread_not_found(thread_id) from exc
        except errors.AvatarMessageError:
            raise
        except Exception as exc:
            reason = str(getattr(exc, "reason", "") or "")
            if reason == "pending_input_queue_full":
                raise errors.invalid_request("pending_input_queue_full") from exc
            logger.exception("avatar submit failed for thread_id=%s", thread_id)
            raise errors.run_failed(f"avatar submit failed: {type(exc).__name__}") from exc
        return AvatarChatAccepted(
            thread_id=thread_id,
            run_id=run_id,
            websocket_url=f"/ws/avatar/v1/threads/{thread_id}",
            server_time=datetime.now(UTC),
        )

    def _require_thread_manager(self) -> Any:
        """读取 ThreadManagerProtocol 实例。

        关键输入：构造时注入的 thread manager。
        关键输出：可调用 create_thread/boot_or_attach 的对象。
        """
        if self._thread_manager is None:
            raise errors.invalid_request("avatar thread manager is not configured")
        return self._thread_manager

    async def _resolve_thread(self, tm: Any, request: AvatarChatRequest) -> Any:
        """校验已有 thread 或创建新的 generic_chat thread。

        关键输入：ThreadManager 和 AvatarChatRequest。
        关键输出：generic_chat ThreadMetadata。
        """
        if request.thread_id:
            return self._require_existing_generic_thread(tm, request.thread_id)

        preset_id = (request.preset_id or "").strip()
        if not preset_id:
            raise errors.preset_required()
        try:
            return await tm.create_thread(
                "Avatar",
                preset_id,
                backend_kind="generic_chat",
                cwd=request.cwd,
            )
        except errors.AvatarMessageError:
            raise
        except ValueError as exc:
            raise errors.invalid_request(str(exc)) from exc
        except Exception as exc:
            logger.exception("avatar create_thread failed")
            raise errors.run_failed(f"avatar thread create failed: {type(exc).__name__}") from exc

    def _require_existing_generic_thread(self, tm: Any, thread_id: str) -> Any:
        """确认已有 thread 存在且属于 generic_chat。

        关键输入：ThreadManager 和 thread_id。
        关键输出：匹配的 ThreadMetadata。
        """
        for metadata in tm.list_threads():
            if getattr(metadata, "id", None) != thread_id:
                continue
            if getattr(metadata, "backend_kind", "generic_chat") != "generic_chat":
                raise errors.invalid_thread(thread_id)
            return metadata
        raise errors.thread_not_found(thread_id)

    async def _ensure_runtime_current(self, tm: Any, thread_id: str) -> None:
        """确保已启动 cell 的 runtime preset 与 metadata 一致。

        关键输入：ThreadManager 和 thread_id。
        关键输出：刷新成功则返回；失败抛 AvatarMessageError。
        """
        ensure_runtime = getattr(tm, "ensure_cell_runtime_preset_current", None)
        if not callable(ensure_runtime):
            return
        try:
            refreshed = await ensure_runtime(thread_id)
        except Exception as exc:
            logger.exception("avatar runtime refresh raised for thread_id=%s", thread_id)
            raise errors.runtime_refresh_failed(thread_id) from exc
        if refreshed is False:
            raise errors.runtime_refresh_failed(thread_id)

    @staticmethod
    def _attachments_to_dicts(request: AvatarChatRequest) -> list[dict[str, Any]] | None:
        """把附件 DTO 转换为 run_once 使用的 dict 列表。

        关键输入：AvatarChatRequest.attachments。
        关键输出：dict 列表或 None。
        """
        if not request.attachments:
            return None
        return [attachment.model_dump() for attachment in request.attachments]

    @staticmethod
    def _new_run_id(thread_id: str) -> str:
        """生成 REST accepted 可返回的 run id。

        关键输入：thread_id。
        关键输出：fake-friendly 的短 run id 字符串。
        """
        return f"avatar-{thread_id}-{secrets.token_hex(4)}"


__all__ = ["AvatarAssistantManager"]
