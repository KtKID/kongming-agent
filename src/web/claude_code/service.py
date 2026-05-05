"""Claude SDK 调用核心（v0.1）。

参考 ccui ``server/claude-sdk.js::queryClaudeSDK``（``mapCliOptionsToSDK``
+ ``query()`` 调用 + for-await 循环）。

职责：

- 装配 ``ClaudeAgentOptions``：强制 ``include_partial_messages=True`` [E9]，
  透传 model / cwd / permission_mode / allowed_tools / disallowed_tools /
  resume；可选 ``contextPackPath`` → ``SystemPromptFile``；注入 ``can_use_tool``
- 多 run 复用同一 ``ClaudeSDKClient``：``_clients[session_id]`` 缓存
- 主循环 ``async for msg in client.query(...)`` → ``normalizer.normalize`` →
  ``writer.send_json``
- ``abort(session_id)``：调 :meth:`SessionManager.request_abort`（cancel
  query_task + set abort_event）
- ``shutdown_all()``：进程关停时释放所有 ``_clients`` 资源
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from web._shared.session_manager import SessionManager
from web.claude_code.approval import ApprovalBridge
from web.claude_code.normalizer import ClaudeNormalizer

logger = logging.getLogger(__name__)

# thread_id 形如 thread-<12 hex>；其他形态（pending-XXX placeholder）跳过 sdk_session_id
# 落盘。与 web.routers.threads.THREAD_ID_RE / web.claude_code.route._THREAD_ID_RE 同步。
_THREAD_ID_RE: re.Pattern[str] = re.compile(r"^thread-[a-f0-9]{12}$")


class ClaudeCodeService:
    """ClaudeSDKClient 包装 + 多 run 复用 + 流式输出。

    生命周期：每个 WebSocket 连接一个独立实例（per-connection），但
    内部 ``_clients`` 跨 query 复用——同一 session_id 的多次 ``claude-command``
    走同一个 ``ClaudeSDKClient``。
    """

    def __init__(
        self,
        normalizer: ClaudeNormalizer,
        approval: ApprovalBridge,
        sessions: SessionManager,
        *,
        client_factory: Any = None,
        thread_manager: Any = None,
    ) -> None:
        """v0.2 新增 ``thread_manager`` 注入。

        ``None`` 表示老路径（v0.1 行为，不做 thread metadata 持久化 / 自动 resume）。
        """
        self._normalizer = normalizer
        self._approval = approval
        self._sessions = sessions
        self._clients: dict[str, ClaudeSDKClient] = {}
        # 注入用，单测可以替换；默认走 SDK 类
        self._client_factory: Any = (
            client_factory if client_factory is not None else ClaudeSDKClient
        )
        self._thread_manager: Any = thread_manager

    # ----- 主入口 -----

    async def query(
        self,
        command: str,
        options: dict[str, Any],
        writer: Any,
        *,
        register_id_override: str | None = None,
    ) -> None:
        """发起一次 Claude run。

        步骤：

        1. 装配 ``ClaudeAgentOptions``
        2. 复用或新建 ``ClaudeSDKClient``
        3. ``approval.set_active_writer(writer)`` 让 can_use_tool 能 emit
        4. 注册 session
        5. ``async for msg in client.query(prompt=command)`` → normalize → send
        6. finally：清 active_writer

        Args:
            register_id_override: v0.1.6 thread-bound 路径用，优先级高于
                ``options["sessionId"]`` 与内部 placeholder。让上层（``/ws/claude-code``
                带 thread_id 时）显式以 thread_id 注册 SessionManager，避免
                ``pending-XXX`` placeholder 与前端 session id 不一致的歧义。
                ``None`` 表示走原有逻辑（保留对未传 thread_id 调用方的兼容）。
        """
        session_id = options.get("sessionId") if isinstance(options, dict) else None

        # v0.2 自动 resume：thread-bound 路径 + thread metadata 已绑定 sdk_session_id →
        # 注入 resume + cwd（用户显式 resume 不被覆盖）
        if (
            register_id_override
            and self._thread_manager is not None
            and not (isinstance(options, dict) and "resume" in options)
        ):
            with contextlib.suppress(Exception):
                metas = self._thread_manager.list_threads()
                meta = next((m for m in metas if m.id == register_id_override), None)
                sdk_sid = getattr(meta, "sdk_session_id", "") if meta is not None else ""
                if sdk_sid:
                    options = {
                        **(options if isinstance(options, dict) else {}),
                        "resume": sdk_sid,
                    }
                    cwd_val = getattr(meta, "cwd", "") if meta is not None else ""
                    if cwd_val and "cwd" not in options:
                        options["cwd"] = cwd_val

        # 1. 装配 options
        opts = self._build_options(options)

        # 2. 复用或新建 client（connect 失败也要走 error 路径）
        client = self._clients.get(session_id) if session_id else None
        is_new_client = client is None
        register_id: str | None = None
        record: Any = None
        try:
            if client is None:
                client = self._client_factory(options=opts)
                await client.connect()
                if session_id:
                    self._clients[session_id] = client

            # 3. set active writer
            self._approval.set_active_writer(writer)

            # 4. 注册 session：
            #    优先级 register_id_override > options["sessionId"] > placeholder。
            #    v0.1.6 thread-bound 路径走 override，让 thread_id 直接当 session id
            #    用，避免 SystemMessage(init) 之前 placeholder 与前端不一致的歧义。
            register_id = register_id_override or session_id or f"pending-{id(client)}"
            record = await self._sessions.register(register_id, writer)

            # 5. 主循环——把 async for 包成 task 以便能被 abort cancel
            query_task = asyncio.create_task(
                self._consume(client, command, writer, register_id),
            )
            record.query_task = query_task
            await query_task
        except asyncio.CancelledError:
            logger.info("claude-code service.query cancelled (session=%s)", register_id)
            # 通知前端 aborted
            with contextlib.suppress(Exception):
                await writer.send_json(
                    {
                        "kind": "complete",
                        "provider": "claude",
                        "sessionId": register_id,
                        "aborted": True,
                        "exitCode": 1,
                    },
                )
        except Exception as exc:
            logger.exception("claude-code service.query failed")
            with contextlib.suppress(Exception):
                await writer.send_json(
                    {
                        "kind": "error",
                        "provider": "claude",
                        "sessionId": register_id,
                        "error": str(exc),
                    },
                )
            # 失败时清掉缓存的 client，下次 query 重建
            if (
                client is not None
                and is_new_client
                and session_id
                and self._clients.get(session_id) is client
            ):
                with contextlib.suppress(Exception):
                    await client.disconnect()
                self._clients.pop(session_id, None)
        finally:
            # 6. 清 active writer + unregister
            self._approval.clear_active_writer()
            if register_id is not None:
                await self._sessions.unregister(register_id)

    # ----- 中止 / 关停 -----

    async def abort(self, session_id: str) -> bool:
        """请求中止指定 session 的当前 run。

        通过 :class:`SessionManager` 设 abort_event + cancel query_task。
        正在跑的 ``query`` 主循环会捕到 CancelledError 退出。
        """
        return await self._sessions.request_abort(session_id)

    async def shutdown_all(self) -> None:
        """进程关停：释放所有缓存的 ``ClaudeSDKClient``。"""
        for sid, client in list(self._clients.items()):
            with contextlib.suppress(Exception):
                await client.disconnect()
            self._clients.pop(sid, None)

    # ----- 私有辅助 -----

    async def _consume(
        self,
        client: ClaudeSDKClient,
        command: str,
        writer: Any,
        register_id: str,
    ) -> None:
        """主流式循环——单独包成方法以便能用 task.cancel() 强制中断。

        关键行为：检测到第一条 ``session_created`` 时把 SessionManager 表里的
        placeholder 改名成真实 SDK session_id，并把后续所有出站消息的
        ``sessionId`` 字段重写为真实 id——避免前端拿到 placeholder（e2e smoke
        暴露的 Bug 1）。
        """
        # 发 prompt（query 发起后立即拿 receive_response 也可，但 ccui 走 query 形式）
        await client.query(prompt=command)

        # 当前用于出站消息 sessionId 字段的真值——首次见到 session_created 后切换
        active_sid = register_id

        async for msg in client.receive_response():
            normalized = self._normalizer.normalize(msg, active_sid)
            for n in normalized:
                # 第一次见到 session_created → 把 placeholder 改名成真实 SDK id
                if n.get("kind") == "session_created":
                    new_id = n.get("newSessionId")
                    if isinstance(new_id, str) and new_id and active_sid != new_id:
                        renamed = await self._sessions.rename(active_sid, new_id)
                        if renamed:
                            active_sid = new_id
                    # v0.2 落盘 sdk_session_id（thread-bound 路径才做；
                    # placeholder pending-XXX 跳过；invariant：仅未绑定时落盘）
                    if (
                        self._thread_manager is not None
                        and isinstance(new_id, str)
                        and new_id
                        and self._is_thread_id(register_id)
                    ):
                        try:
                            metas = self._thread_manager.list_threads()
                            meta = next((m for m in metas if m.id == register_id), None)
                            if meta is not None and not getattr(meta, "sdk_session_id", ""):
                                await self._thread_manager.bind_sdk_session(
                                    register_id,
                                    new_id,
                                    getattr(meta, "cwd", "") or "",
                                )
                        except Exception:
                            # 已绑定 / 冲突等异常不影响主对话流
                            logger.warning(
                                "claude-code _consume bind_sdk_session failed",
                                exc_info=True,
                            )
                # 把出站消息的 sessionId 字段同步成真实 id
                if n.get("sessionId") != active_sid:
                    n["sessionId"] = active_sid
                with contextlib.suppress(Exception):
                    await writer.send_json(dict(n))

    @staticmethod
    def _is_thread_id(s: str) -> bool:
        """thread_id 形如 ``thread-<12 hex>``；其他形态（pending-XXX placeholder）
        跳过 sdk_session_id 落盘。"""
        return bool(_THREAD_ID_RE.match(s))

    def _build_options(self, options: dict[str, Any]) -> ClaudeAgentOptions:
        """把前端传来的 dict options 装配成 SDK ``ClaudeAgentOptions``。

        强制开启 ``include_partial_messages=True``——这是 [E9 决议] 的硬约束
        （否则没流式 delta）。
        """
        kwargs: dict[str, Any] = {
            "include_partial_messages": True,
            "can_use_tool": self._approval.can_use_tool,
        }

        if not isinstance(options, dict):
            return ClaudeAgentOptions(**kwargs)

        if (model := options.get("model")) is not None:
            kwargs["model"] = model
        if (cwd := options.get("cwd")) is not None:
            kwargs["cwd"] = cwd
        if (
            permission_mode := options.get("permissionMode") or options.get("permission_mode")
        ) is not None:
            kwargs["permission_mode"] = permission_mode
        if (
            allowed_tools := options.get("allowedTools") or options.get("allowed_tools")
        ) is not None:
            kwargs["allowed_tools"] = list(allowed_tools)
        if (
            disallowed_tools := options.get("disallowedTools") or options.get("disallowed_tools")
        ) is not None:
            kwargs["disallowed_tools"] = list(disallowed_tools)
        if (resume := options.get("resume")) is not None:
            kwargs["resume"] = resume

        # contextPackPath → SystemPromptFile（dict 形式：SDK 接受 dict 形态的
        # SystemPrompt union；详见 sidecar sdk_bridge._build_system_prompt）
        context_pack_path = options.get("contextPackPath") or options.get("context_pack_path")
        if context_pack_path:
            kwargs["system_prompt"] = {
                "type": "file",
                "path": str(context_pack_path),
            }

        return ClaudeAgentOptions(**kwargs)


__all__ = ["ClaudeCodeService"]
