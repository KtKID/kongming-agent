"""Claude Code 通道内部审批协议。

该协议把 Web route/service 与安全模块的具体实现隔离。通道层只依赖 SDK 回调
所需的方法集合；``ApprovalRuntimeManager`` 负责装配共享安全链和具体 bridge。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from hosts.web.integrations.claude_code.normalizer import ClaudeNormalizer
from hosts.web.shared.session_manager import SessionManager


class ClaudeApprovalProtocol(Protocol):
    """Claude service 调用审批 bridge 所需的最小接口。"""

    def set_active_writer(self, writer: object) -> None:
        """绑定当前 query writer。"""

    def clear_active_writer(self) -> None:
        """清理当前 query writer。"""

    def set_active_cwd(self, cwd: str) -> None:
        """更新当前工具请求的工作目录。"""

    async def can_use_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """把 SDK 工具请求交给共享审批链。"""


@runtime_checkable
class ClaudeApprovalFactoryProtocol(Protocol):
    """Claude route 从 Web 装配门户获取 per-connection bridge 的接口。"""

    def build_claude_bridge(
        self,
        normalizer: ClaudeNormalizer,
        sessions: SessionManager,
        *,
        cwd: str,
        thread_id: str,
    ) -> ClaudeApprovalProtocol:
        """构造绑定 thread/cwd 的 Claude 审批 bridge。"""


__all__ = ["ClaudeApprovalFactoryProtocol", "ClaudeApprovalProtocol"]
