"""Claude Agent SDK 工具审批适配器。

SDK 工具名和参数先转换为 Kongming canonical ``ApprovalRequest``，随后进入
共享 ``SafetyGatedApproval``。审批规则、HardBlock、记忆写回和交互队列均由
安全模块门户负责；本适配器只完成协议翻译与 SDK 结果翻译。
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from core.contracts import ApprovalProvider, ApprovalRequest
from hosts.web.integrations.claude_code.normalizer import ClaudeNormalizer
from hosts.web.shared.session_manager import SessionManager

logger = logging.getLogger(__name__)

_CLAUDE_TOOL_NAMES: dict[str, str] = {
    "Bash": "run_shell",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "Glob": "list_dir",
    "Grep": "search_files",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "Skill": "skill",
    "Task": "run_agent_workflow",
}


def canonicalize_claude_tool_request(
    sdk_name: str,
    tool_input: dict[str, Any],
    *,
    run_id: str,
    session_id: str,
    call_id: str,
    cwd: str,
    thread_id: str,
) -> ApprovalRequest:
    """把 Claude SDK 工具调用转换成共享审批合同。"""
    tool_name = _CLAUDE_TOOL_NAMES.get(sdk_name, sdk_name)
    arguments = dict(tool_input)
    if tool_name in {"read_file", "write_file", "edit_file"}:
        file_path = arguments.pop("file_path", None)
        if isinstance(file_path, str):
            arguments["path"] = file_path
    return ApprovalRequest(
        run_id=run_id,
        session_id=session_id,
        turn=0,
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        metadata={
            "channel": "claude_code",
            "cwd": cwd,
            "thread_id": thread_id,
            "sdk_tool_name": sdk_name,
        },
    )


class ApprovalBridge:
    """把 Claude SDK permission callback 接入共享审批 provider。"""

    def __init__(
        self,
        normalizer: ClaudeNormalizer,
        sessions: SessionManager,
        *,
        approval: ApprovalProvider,
        cwd: str = "",
        thread_id: str | None = None,
    ) -> None:
        self._normalizer = normalizer
        self._sessions = sessions
        self._approval = approval
        self._active_writer: object | None = None
        self._active_cwd = cwd
        self._thread_id = thread_id

    def set_active_writer(self, writer: object) -> None:
        """记录当前 query writer，供 SessionManager 反查 session 坐标。"""
        self._active_writer = writer

    def clear_active_writer(self) -> None:
        """清理当前 query writer 引用。"""
        self._active_writer = None

    def set_active_cwd(self, cwd: str) -> None:
        """更新 options 覆盖后的工作目录。"""
        if cwd:
            self._active_cwd = cwd

    @property
    def active_cwd(self) -> str:
        """返回当前 canonical 请求使用的工作目录。"""
        return self._active_cwd

    async def can_use_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """转换 SDK 请求，执行共享审批并翻译回 SDK 结果。"""
        request_id = ctx.tool_use_id
        if request_id is None:
            return PermissionResultDeny(message="missing tool_use_id; permission denied")

        session_id = self._infer_session_id() or self._thread_id or request_id
        thread_id = self._thread_id or session_id
        request = canonicalize_claude_tool_request(
            tool_name,
            tool_input,
            run_id=f"claude:{session_id}",
            session_id=session_id,
            call_id=request_id,
            cwd=self._active_cwd,
            thread_id=thread_id,
        )
        decision = await self._approval.decide(request)
        if decision.outcome == "approved":
            return PermissionResultAllow(updated_input=tool_input)

        self._normalizer.add_pending_deny(request_id)
        return PermissionResultDeny(message=decision.reason or "Tool use denied")

    def _infer_session_id(self) -> str | None:
        """从 SessionManager 反查当前 writer 对应的 session id。"""
        active = self._active_writer
        if active is None:
            return None
        for session_id in self._sessions.list_active():
            record = self._sessions.get(session_id)
            if record is not None and record.writer is active:
                return session_id
        return None


__all__ = ["ApprovalBridge", "canonicalize_claude_tool_request"]
