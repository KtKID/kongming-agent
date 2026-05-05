"""审批桥接（v0.1）。

把 SDK 的同步 ``can_use_tool`` 回调跟 WebSocket 异步 round-trip 桥接：

1. SDK 调 ``can_use_tool(tool_name, input, ctx)``
2. 检查 ``_allow_list`` —— 命中则直接 allow
3. 否则 emit ``permission_request`` 到当前 writer
4. 创建 ``asyncio.Future``，挂在 ``_pending[request_id]``
5. await Future 拿前端响应（``resolve(request_id, decision)`` 触发）
6. allow → 返回 ``PermissionResultAllow``；deny → 通知 normalizer 去重 +
   返回 ``PermissionResultDeny``

参考 ccui ``server/claude-sdk.js`` 第 530-600 行 ``canUseTool`` 实现。

设计要点：

- ``_active_writer``：service.query 调用前注入；can_use_tool 走这个 writer
  emit permission_request；service.query 退出后清空
- ``_allow_list``：内存 list（thread 级生命周期），重启进程即丢；ccui 同款
- ``_matches`` 静态方法：精确名匹配 + ``Bash(prefix:*)`` 通配（不实现完整 glob）
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from web._shared.session_manager import SessionManager
from web.claude_code.normalizer import ClaudeNormalizer

logger = logging.getLogger(__name__)

# 形如 ``Bash(npm:*)`` —— prefix 匹配 ccui 简化通配
_BASH_PERMISSION_RE = re.compile(r"^Bash\((.+):\*\)$")


class ApprovalBridge:
    """审批桥接器（per-connection）。

    生命周期：每个 WebSocket 连接一个独立实例，避免跨连接的
    Future / allow_list 污染。
    """

    def __init__(
        self,
        normalizer: ClaudeNormalizer,
        sessions: SessionManager,
    ) -> None:
        self._normalizer = normalizer
        self._sessions = sessions
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._allow_list: list[str] = []
        self._active_writer: Any = None

    # ----- 公共接口 -----

    def set_active_writer(self, writer: Any) -> None:
        """注入当前 query 的 writer（service.query 调用前必须先调此）。"""
        self._active_writer = writer

    def clear_active_writer(self) -> None:
        """清空当前 writer（service.query 退出 finally 必须调此）。"""
        self._active_writer = None

    @property
    def allow_list(self) -> list[str]:
        """暴露 allow_list 副本，单测断言用。"""
        return list(self._allow_list)

    def resolve(self, request_id: str, decision: dict[str, Any]) -> bool:
        """前端发回 ``claude-permission-response`` 时由 route 调用。

        Args:
            request_id: 原始 ``ctx.tool_use_id`` —— ``permission_request.requestId``
            decision: 形如 ``{allow: bool, updatedInput?, message?, rememberEntry?}``

        Returns:
            ``True`` 找到 pending Future 并已 set_result；``False`` request_id
            未知（重复 resolve / 超时后 / 状态污染）
        """
        future = self._pending.pop(request_id, None)
        if future is None:
            logger.warning("approval.resolve: unknown request_id=%s", request_id)
            return False
        if not future.done():
            future.set_result(decision)
            return True
        return False

    async def can_use_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """SDK ``can_use_tool`` 回调入口。

        步骤详见模块 docstring。
        """
        # 1. 短路：allow_list 命中
        for entry in self._allow_list:
            if self._matches(entry, tool_name, tool_input):
                return PermissionResultAllow(updated_input=tool_input)

        # SDK ToolPermissionContext.tool_use_id 类型是 str | None；缺失时无法做
        # Future 桥接，直接 deny 兜底（实测 SDK 总会带，但类型上得 narrow）
        request_id = ctx.tool_use_id
        if request_id is None:
            logger.error(
                "approval.can_use_tool: ctx.tool_use_id missing (tool=%s)",
                tool_name,
            )
            return PermissionResultDeny(
                message="missing tool_use_id; permission denied",
            )

        # 2. emit permission_request
        writer = self._active_writer
        if writer is None:
            # 没有 active writer 时无法走 round-trip，直接 deny 兜底
            logger.error(
                "approval.can_use_tool: no active writer (tool=%s, request_id=%s)",
                tool_name,
                request_id,
            )
            return PermissionResultDeny(
                message="no active writer; permission denied",
            )

        # 找到当前 session_id（ctx 没暴露 session id，从 sessions 表反查）
        session_id = self._infer_session_id()

        permission_msg: dict[str, Any] = {
            "kind": "permission_request",
            "provider": "claude",
            "requestId": request_id,
            "toolName": tool_name,
            "input": tool_input,
            "sessionId": session_id,
        }

        try:
            await writer.send_json(permission_msg)
        except Exception as exc:
            # writer 已断 → 兜底 deny
            logger.warning(
                "approval.can_use_tool: writer.send_json failed: %s",
                exc,
            )
            return PermissionResultDeny(message="writer disconnected")

        # 3. 创建 Future + 等响应
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future

        try:
            decision = await future
        except asyncio.CancelledError:
            # 中断（例如 abort）：丢掉 pending 后向上传
            self._pending.pop(request_id, None)
            raise
        finally:
            self._pending.pop(request_id, None)

        # 4. 处理 decision
        if decision.get("allow"):
            remember = decision.get("rememberEntry")
            if isinstance(remember, str) and remember and remember not in self._allow_list:
                self._allow_list.append(remember)
            updated_input = decision.get("updatedInput")
            return PermissionResultAllow(
                updated_input=updated_input if updated_input is not None else tool_input,
            )

        # deny 路径：通知 normalizer 去重，返回 deny
        self._normalizer.add_pending_deny(request_id)
        message = decision.get("message") or "User denied tool use"
        return PermissionResultDeny(message=message)

    # ----- 私有辅助 -----

    def _infer_session_id(self) -> str | None:
        """从 sessions 表里反查当前 writer 对应的 session_id。

        没找到返回 None——permission_request 的 sessionId 字段允许 null。
        """
        active = self._active_writer
        if active is None:
            return None
        for sid in self._sessions.list_active():
            record = self._sessions.get(sid)
            if record is not None and record.writer is active:
                return sid
        return None

    @staticmethod
    def _matches(entry: str, tool_name: str, tool_input: Any) -> bool:
        """ccui 简化匹配：精确名 或 ``Bash(prefix:*)`` 通配。"""
        if not entry or not tool_name:
            return False
        if entry == tool_name:
            return True

        bash_match = _BASH_PERMISSION_RE.match(entry)
        if tool_name == "Bash" and bash_match is not None:
            allowed_prefix = bash_match.group(1)
            command = ""
            if isinstance(tool_input, str):
                command = tool_input.strip()
            elif isinstance(tool_input, dict):
                cmd = tool_input.get("command")
                if isinstance(cmd, str):
                    command = cmd.strip()
            if not command:
                return False
            return command.startswith(allowed_prefix)
        return False


__all__ = ["ApprovalBridge"]
