"""Avatar 对话能力门户。

本脚本实现 AvatarAssistantManager，把 XSpace Avatar chat 请求接入现有
generic_chat thread 运行链路。关键流程是校验或创建 thread，复用
ThreadManager.boot_or_attach 与 bridge.run_once 启动普通对话，再返回 REST
accepted 响应。关键函数职责：capabilities 输出能力声明，chat 处理 REST 首发
和已有 thread 消息，approval 仍由 Avatar WS 透传给 ThreadManager。
"""

from __future__ import annotations

import asyncio
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
    ThreadManager/SessionBridge 链路。
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

        try:
            cell = await tm.boot_or_attach(thread_id)
        except KeyError as exc:
            raise errors.thread_not_found(thread_id) from exc
        except errors.AvatarMessageError:
            raise
        except Exception as exc:
            logger.exception("avatar boot_or_attach failed for thread_id=%s", thread_id)
            raise errors.run_failed(f"avatar boot failed: {type(exc).__name__}") from exc

        await self._ensure_runtime_current(tm, thread_id)
        attachments = self._attachments_to_dicts(request)
        run_id = self._new_run_id(thread_id)
        self._start_run_task(
            cell,
            request.text,
            run_id=run_id,
            reasoning_effort=request.reasoning_effort,
            attachments=attachments,
        )
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

    def _start_run_task(
        self,
        cell: Any,
        text: str,
        *,
        run_id: str,
        reasoning_effort: str | None,
        attachments: list[dict[str, Any]] | None,
    ) -> None:
        """启动后台 generic_chat run_once task。

        关键输入：ThreadCell、用户文本、run_id、reasoning effort 和附件。
        关键输出：cell.current_run_task 被设置为新 task。
        """
        run_once = getattr(cell.bridge, "run_once", None)
        if not callable(run_once):
            raise errors.run_failed("avatar bridge does not expose run_once")

        task = asyncio.create_task(
            self._run_once_safely(
                cell,
                text,
                reasoning_effort=reasoning_effort,
                attachments=attachments,
            ),
            name=f"avatar-run-once-{getattr(cell, 'thread_id', 'unknown')}",
        )
        cell.current_run_task = task

        def _clear_run_task(
            finished: asyncio.Task[None],
            *,
            _cell: Any = cell,
            _task: asyncio.Task[None] = task,
            _run_id: str = run_id,
        ) -> None:
            if getattr(_cell, "current_run_task", None) is _task:
                _cell.current_run_task = None
            try:
                finished.result()
            except asyncio.CancelledError:
                logger.info("avatar run cancelled: run_id=%s", _run_id)
            except Exception:
                logger.exception("avatar run failed after accepted: run_id=%s", _run_id)

        task.add_done_callback(_clear_run_task)

    async def _run_once_safely(
        self,
        cell: Any,
        text: str,
        *,
        reasoning_effort: str | None,
        attachments: list[dict[str, Any]] | None,
    ) -> None:
        """执行 bridge.run_once 并把异常留在后台 task 日志中。

        关键输入：ThreadCell、文本、reasoning effort 和附件。
        关键输出：普通 generic_chat run 完成；异常由 task callback 记录。
        """
        await cell.bridge.run_once(
            text,
            reasoning_effort=reasoning_effort,
            attachments=attachments,
        )
        touch = getattr(cell, "touch", None)
        if callable(touch):
            touch()


__all__ = ["AvatarAssistantManager"]
