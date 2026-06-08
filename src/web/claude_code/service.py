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
import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from network.network_log import log_network_exception
from web._shared.session_manager import SessionManager
from web.claude_code._attachment_prefix import AttachmentPrefixBuilder
from web.claude_code.approval import ApprovalBridge
from web.claude_code.normalizer import ClaudeNormalizer
from web.websocket.thread_status import get_broadcaster

if TYPE_CHECKING:
    from web.protocol.rest_models import UserInputAttachment
    from web.uploads.storage import AssetStorage

logger = logging.getLogger(__name__)

# thread_id 形如 thread-<12 hex>；其他形态（pending-XXX placeholder）跳过 claude_thread_id
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
        asset_storage: AssetStorage | None = None,
    ) -> None:
        """v0.2 新增 ``thread_manager`` 注入；claude-code-channel-image-paste 新增
        ``asset_storage`` 注入（用于附件 ``@file`` prompt 前缀拼接）。

        ``thread_manager=None``：v0.1 行为不做 thread metadata 持久化 / 自动 resume。
        ``asset_storage=None``：不支持图片附件（attachments kwarg 会被忽略）；测试
            stub 可不传，prod 装配在 ``route.py`` 注入 ``app.state.asset_storage``。
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
        # per-thread run 计数器——每次 query 调用自增，用于构造 run_id
        self._run_counters: dict[str, int] = {}
        # attachment prefix builder：注入 storage 后 lazy 构造一次复用
        self._prefix_builder: AttachmentPrefixBuilder | None = (
            AttachmentPrefixBuilder(asset_storage) if asset_storage is not None else None
        )

    # ----- 主入口 -----

    async def query(
        self,
        command: str,
        options: dict[str, Any],
        writer: Any,
        *,
        register_id_override: str | None = None,
        attachments: Sequence[UserInputAttachment] | None = None,
    ) -> None:
        """发起一次 Claude run。

        步骤：

        0. (新) 如有 ``attachments`` + ``_prefix_builder`` + ``register_id_override``，
           调 :meth:`AttachmentPrefixBuilder.build` 把 ``@<abs_path>`` 前缀拼到
           ``command`` 头部；Claude Code SDK subprocess 端 ``attachments.ts`` 会
           grep 这些 ``@<path>`` 引用，自动通过 ``FileReadTool`` 读图喂给 Claude。
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
            attachments: claude-code-channel-image-paste 新增。当
                ``register_id_override`` + ``asset_storage`` + ``attachments`` 三者
                都到位时，按顺序把图片本地路径以 ``@<abs_path>`` 形式拼到 prompt
                头部；缺任一条件就 fallback 到原纯文本 ``command``，向后兼容。
        """
        # 0. (新) attachment prefix 拼接——三者齐全才生效，否则保持原 command
        if attachments and self._prefix_builder is not None and register_id_override:
            prefix = self._prefix_builder.build(attachments, thread_id=register_id_override)
            if prefix:
                command = prefix + command

        session_id = options.get("sessionId") if isinstance(options, dict) else None

        # v0.2 自动 resume：thread-bound 路径 + thread metadata 已绑定 claude_thread_id →
        # 注入 resume + cwd（用户显式 resume 不被覆盖）
        if register_id_override and self._thread_manager is not None:
            try:
                metas = self._thread_manager.list_threads()
                meta = next((m for m in metas if m.id == register_id_override), None)
                if meta is not None:
                    cwd_val = getattr(meta, "cwd", "")
                    if cwd_val and (not isinstance(options, dict) or "cwd" not in options):
                        options = {**(options if isinstance(options, dict) else {}), "cwd": cwd_val}
                    claude_tid = getattr(meta, "claude_thread_id", "")
                    if claude_tid and not (isinstance(options, dict) and "resume" in options):
                        options = {
                            **(options if isinstance(options, dict) else {}),
                            "resume": claude_tid,
                        }
            except Exception as exc:
                log_network_exception(
                    "web.claude_code.service",
                    "autoresume_lookup_failed",
                    exc,
                    register_id_override=register_id_override,
                )

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

            # 3. set active writer + cwd（cwd 可能被 options 覆盖；smart-approval-v1 用）
            self._approval.set_active_writer(writer)
            if isinstance(options, dict):
                effective_cwd = options.get("cwd")
                if isinstance(effective_cwd, str) and effective_cwd:
                    self._approval.set_active_cwd(effective_cwd)

            # 4. 注册 session：
            #    优先级 register_id_override > options["sessionId"] > placeholder。
            #    v0.1.6 thread-bound 路径走 override，让 thread_id 直接当 session id
            #    用，避免 SystemMessage(init) 之前 placeholder 与前端不一致的歧义。
            register_id = register_id_override or session_id or f"pending-{id(client)}"
            record = await self._sessions.register(register_id, writer)

            # run_index：per-thread 自增，用于构造 run_id（跟 core.Runner 一致）
            run_index = self._run_counters.get(register_id, 0) + 1
            self._run_counters[register_id] = run_index

            # 5. 主循环——把 async for 包成 task 以便能被 abort cancel
            query_task = asyncio.create_task(
                self._consume(client, command, writer, register_id, run_index=run_index),
            )
            record.query_task = query_task
            await query_task
        except asyncio.CancelledError:
            logger.info("claude-code service.query cancelled (session=%s)", register_id)
            # 通知前端 aborted
            try:
                await writer.send_json(
                    {
                        "frame_type": "complete",
                        "provider": "claude",
                        "sessionId": register_id,
                        "aborted": True,
                        "exitCode": 1,
                    },
                )
            except Exception as exc:
                log_network_exception(
                    "web.claude_code.service",
                    "send_aborted_complete_failed",
                    exc,
                    session_id=register_id,
                )
        except Exception as exc:
            logger.exception("claude-code service.query failed")
            try:
                await writer.send_json(
                    {
                        "frame_type": "error",
                        "provider": "claude",
                        "sessionId": register_id,
                        "error": str(exc),
                    },
                )
            except Exception as send_exc:
                log_network_exception(
                    "web.claude_code.service",
                    "send_query_error_failed",
                    send_exc,
                    session_id=register_id,
                )
            # 失败时清掉缓存的 client，下次 query 重建
            if (
                client is not None
                and is_new_client
                and session_id
                and self._clients.get(session_id) is client
            ):
                try:
                    await client.disconnect()
                except Exception as disconnect_exc:
                    log_network_exception(
                        "web.claude_code.service",
                        "client_disconnect_failed",
                        disconnect_exc,
                        session_id=session_id,
                    )
                self._clients.pop(session_id, None)
        finally:
            # 6. 清 active writer + unregister
            self._approval.clear_active_writer()
            if register_id is not None:
                await self._sessions.unregister(register_id)

    # ----- 中止 / 关停 -----

    async def abort(self, session_id: str) -> bool:
        """请求中止指定 session 的当前 run。

        interrupt-claude-channel-v0.1：SDK 原生 interrupt + task.cancel 兜底。

        1. SDK 路径（主）：调 ``ClaudeSDKClient.interrupt()`` 通过 control_request
           通知 CLI 子进程 → QueryEngine.abortController.abort() → AbortController
           树级联打断所有子 agent（含递归 subagent + 它们的 Bash subprocess）
        2. task.cancel() 路径（兜底）：调 :meth:`SessionManager.request_abort`
           强 cancel Python 侧 ``_consume`` 协程，防 SDK 失败 / 子进程僵尸 /
           tool 不响应 abort

        幂等：反复调多次必须无害——``client.interrupt`` 异常吞掉；
        ``request_abort`` 本身幂等（重复 set abort_event + cancel 已 done task 无副作用）。

        v0.2 rename race：``_clients`` 字典 key 可能是 placeholder（首次 query 用
        thread_id / pending-XXX 占位）或真 SDK uuid（``session_created`` 后 rename）；
        前端发 abort 用 sdk_uuid 但 ``_clients[uuid]`` 可能为 None（rename 只动
        ``_sessions`` 没同步到 ``_clients``）。这里通过
        :meth:`_lookup_client_for_abort` 双 key 查 client。

        Args:
            session_id: 前端传来的 sessionId（可能是 placeholder 或 sdk_uuid）

        Returns:
            ``True`` session 存在且已发出 abort；``False`` session 不存在
        """
        # 1. SDK 原生 interrupt（fire-and-forget，防 60s control_request timeout
        #    阻塞 ws 读循环）
        client = self._lookup_client_for_abort(session_id)
        if client is not None:
            try:
                # RUF006 不存引用是有意的——abort 是终态操作，子 agent 树打断
                # 完就完，不需要 await 结果；task 异常已在 _safe_interrupt 内吞
                asyncio.create_task(  # noqa: RUF006
                    self._safe_interrupt(client),
                    name=f"claude-interrupt-{session_id}",
                )
            except Exception:
                logger.exception(
                    "scheduling client.interrupt task failed; tolerate (task.cancel covers)"
                )

        # 2. task.cancel() 兜底
        return await self._sessions.request_abort(session_id)

    def _lookup_client_for_abort(self, session_id: str) -> ClaudeSDKClient | None:
        """双 key 查 ``_clients[session_id]``，处理 v0.2 rename race。

        场景：``SessionManager.rename(placeholder, sdk_uuid)`` 改了
        ``_sessions`` 的 key 与 ``record.session_id``，但 ``_clients`` 字典 key
        未同步。前端发 abort 用 sdk_uuid 时，``_clients.get(uuid)`` 返回 None；
        fallback 用 ``SessionManager`` 反查同 record 下的另一 id 试一次。

        注意：访问 ``self._sessions._sessions`` 是私有字段，没有公开接口
        遍历记录；接受这个临时耦合直到 SessionManager 暴露 iter API。
        """
        # 直接查
        client = self._clients.get(session_id)
        if client is not None:
            return client

        # fallback：遍历 _clients，看哪一项的 key 在 SessionManager 里映射到
        # 同一个 record.session_id（即前端传的 session_id）
        try:
            for cli_key, cli in self._clients.items():
                sm_rec = self._sessions._sessions.get(cli_key)
                if sm_rec is not None and sm_rec.session_id == session_id:
                    return cli
        except Exception:
            logger.debug("_lookup_client_for_abort fallback failed", exc_info=True)
        return None

    @staticmethod
    async def _safe_interrupt(client: ClaudeSDKClient) -> None:
        """后台跑 ``client.interrupt()``，异常吞掉不传播。

        幂等 + 防 control_request 60s timeout 影响调用方。
        ``task.cancel`` 兜底已经在 :meth:`abort` 里 await，所以 SDK
        interrupt 失败不会让 abort 整体失效。
        """
        try:
            await client.interrupt()
        except Exception:
            logger.exception("client.interrupt() raised; tolerate (task.cancel will cover)")

    async def shutdown_all(self) -> None:
        """进程关停：释放所有缓存的 ``ClaudeSDKClient``。"""
        for sid, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception as exc:
                log_network_exception(
                    "web.claude_code.service",
                    "shutdown_disconnect_failed",
                    exc,
                    session_id=sid,
                )
            self._clients.pop(sid, None)

    # ----- 私有辅助 -----

    async def _consume(
        self,
        client: ClaudeSDKClient,
        command: str,
        writer: Any,
        register_id: str,
        *,
        run_index: int = 0,
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
        broadcaster = get_broadcaster()

        async for msg in client.receive_response():
            # AssistantMessage 到达时：从 SDK jsonl 真源派生最新 usage 推前端刷新。
            # **v2**：manager 无状态门面，不接受 push；直接调 get_thread_usage 派生
            # 后广播（前端收到 usage_summary_updated 帧重新渲染）。
            if (
                self._thread_manager is not None
                and self._is_thread_id(register_id)
                and hasattr(msg, "usage")
                and hasattr(msg, "content")  # AssistantMessage 特征：有 content 字段
            ):
                raw_assistant_usage = getattr(msg, "usage", None)
                if isinstance(raw_assistant_usage, dict) and raw_assistant_usage:
                    try:
                        usage_dto = await self._thread_manager.usage_manager.get_thread_usage(
                            register_id
                        )
                        if usage_dto is not None:
                            await broadcaster.broadcast(
                                {
                                    "type": "usage_summary_updated",
                                    "threadId": register_id,
                                    "usage": usage_dto.model_dump(),
                                }
                            )
                    except Exception as exc:
                        log_network_exception(
                            "web.claude_code.service",
                            "assistant_usage_broadcast_failed",
                            exc,
                            session_id=register_id,
                        )

            normalized = self._normalizer.normalize(msg, active_sid)
            for n in normalized:
                # 第一次见到 session_created → 把 placeholder 改名成真实 SDK id
                if n.get("frame_type") == "session_created":
                    new_id = n.get("newSessionId")
                    if isinstance(new_id, str) and new_id and active_sid != new_id:
                        renamed = await self._sessions.rename(active_sid, new_id)
                        if renamed:
                            active_sid = new_id
                    # v0.2 落盘 claude_thread_id（thread-bound 路径才做；
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
                            if meta is not None and not getattr(meta, "claude_thread_id", ""):
                                await self._thread_manager.bind_claude_thread(
                                    register_id,
                                    new_id,
                                    getattr(meta, "cwd", "") or "",
                                )
                        except Exception:
                            # 已绑定 / 冲突等异常不影响主对话流
                            logger.warning(
                                "claude-code _consume bind_claude_thread failed",
                                exc_info=True,
                            )
                # ResultMessage（complete）到达时：从 SDK 真源 jsonl 派生最新 usage
                # 推前端刷新。**v2**：用 manager.get_thread_usage（无状态门面）。
                if (
                    n.get("frame_type") == "complete"
                    and self._thread_manager is not None
                    and self._is_thread_id(register_id)
                ):
                    try:
                        usage_dto = await self._thread_manager.usage_manager.get_thread_usage(
                            register_id
                        )
                        if usage_dto is not None:
                            await broadcaster.broadcast(
                                {
                                    "type": "usage_summary_updated",
                                    "threadId": register_id,
                                    "usage": usage_dto.model_dump(),
                                }
                            )
                    except Exception as exc:
                        log_network_exception(
                            "web.claude_code.service",
                            "complete_usage_broadcast_failed",
                            exc,
                            session_id=register_id,
                        )
                # 把出站消息的 sessionId 字段同步成真实 id
                if n.get("sessionId") != active_sid:
                    n["sessionId"] = active_sid
                try:
                    await writer.send_json(dict(n))
                except Exception as exc:
                    log_network_exception(
                        "web.claude_code.service",
                        "stream_send_failed",
                        exc,
                        session_id=active_sid,
                        frame_kind=n.get("kind"),
                    )
                await broadcaster.emit(register_id, dict(n))

    @staticmethod
    def _is_thread_id(s: str) -> bool:
        """thread_id 形如 ``thread-<12 hex>``；其他形态（pending-XXX placeholder）
        跳过 claude_thread_id 落盘。"""
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
