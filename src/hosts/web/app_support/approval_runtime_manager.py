"""Web 审批运行时装配门户。

``ApprovalRuntimeManager`` 负责构造共享规则门户、共享 pending 门户、Inbox 与
Avatar 事件 sink，并为 Claude Code 通道构造协议 bridge。FastAPI app shell 只
依赖本 Manager，安全模块的具体类型保持在装配边界内。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from hosts.web.app_support.thread_permissions_rest_manager import (
    ThreadPermissionsRestManager,
)
from hosts.web.approvals.auto.audit import AuditLogger
from hosts.web.approvals.global_inbox.broadcaster import ApprovalInboxBroadcaster
from hosts.web.avatar import AvatarManager
from hosts.web.avatar.approval_sink import AvatarApprovalSink
from hosts.web.integrations.claude_code.approval import ApprovalBridge
from hosts.web.integrations.claude_code.contracts import ClaudeApprovalProtocol
from hosts.web.integrations.claude_code.normalizer import ClaudeNormalizer
from hosts.web.shared.session_manager import SessionManager
from hosts.web.threads.types import ThreadManagerProtocol
from infrastructure.config.models import Config
from safety.approval.chain import build_safety_chain
from safety.approval.llm_reviewer import build_approval_llm_reviewer
from safety.approval.manager import ApprovalManager, make_manager_prompt_fn
from safety.approval.permissions_manager import PermissionsManager
from safety.auto_approval.manager import AutoApprovalManager
from safety.auto_approval.policy import AutoApprovalPolicy
from safety.inbox.event_sink import InboxEventSink
from tools.runtime.approval import InteractiveApproval


@dataclass
class ApprovalRuntimeManager:
    """维护 Web 进程共享审批对象并创建通道适配器。"""

    config: Config
    permissions_manager: PermissionsManager
    thread_permissions_rest_manager: ThreadPermissionsRestManager
    approval_manager: ApprovalManager
    auto_approval_manager: AutoApprovalManager
    auto_approval_policy: AutoApprovalPolicy
    auto_approval_audit: AuditLogger

    @classmethod
    def build(
        cls,
        *,
        config: Config,
        kongming_home: Path,
        broadcaster: ApprovalInboxBroadcaster,
        avatar_manager: AvatarManager,
        thread_manager: ThreadManagerProtocol,
    ) -> ApprovalRuntimeManager:
        """构造共享 thread permissions、pending 队列与用户交互 sink。"""
        auto_approval_manager = AutoApprovalManager.build(kongming_home)
        policy = auto_approval_manager.policy
        approval_audit = AuditLogger(auto_approval_manager.root_dir / "audit.jsonl")
        permissions_manager = PermissionsManager(
            kongming_home,
            event_sinks=[approval_audit],
        )
        approval_manager = ApprovalManager(
            permissions_manager=permissions_manager,
            auto_approval_policy=policy,
            llm_reviewer=build_approval_llm_reviewer(config),
            audit_sink=approval_audit,
        )
        approval_manager.register_event_sink(
            InboxEventSink(
                broadcaster=broadcaster,
                manager=approval_manager,
            )
        )
        approval_manager.register_event_sink(AvatarApprovalSink(avatar_manager))
        set_approval_manager = getattr(thread_manager, "set_approval_manager", None)
        if callable(set_approval_manager):
            set_approval_manager(approval_manager)
        set_permissions_manager = getattr(thread_manager, "set_permissions_manager", None)
        if callable(set_permissions_manager):
            set_permissions_manager(permissions_manager)
        return cls(
            config=config,
            permissions_manager=permissions_manager,
            thread_permissions_rest_manager=ThreadPermissionsRestManager(permissions_manager),
            approval_manager=approval_manager,
            auto_approval_manager=auto_approval_manager,
            auto_approval_policy=policy,
            auto_approval_audit=approval_audit,
        )

    def attach_to_app_state(self, app: FastAPI) -> None:
        """把门户与兼容的公共 Manager 引用挂到 FastAPI state。"""
        app.state.approval_runtime_manager = self
        app.state.permissions_manager = self.permissions_manager
        app.state.thread_permissions_manager = self.thread_permissions_rest_manager
        app.state.approval_manager = self.approval_manager
        app.state.auto_approval_manager = self.auto_approval_manager
        app.state.auto_approval_policy = self.auto_approval_policy
        app.state.auto_approval_audit = self.auto_approval_audit

    async def aclose(self) -> None:
        """关闭 pending 审批和 LLM reviewer 的连接池。"""
        await self.approval_manager.aclose()

    def build_claude_bridge(
        self,
        normalizer: ClaudeNormalizer,
        sessions: SessionManager,
        *,
        cwd: str,
        thread_id: str,
    ) -> ClaudeApprovalProtocol:
        """为 Claude WebSocket 构造复用共享安全链的协议 bridge。"""
        prompt_fn = make_manager_prompt_fn(
            self.approval_manager,
            thread_id,
            channel="claude_code",
            default_cwd=cwd,
        )
        shared_approval = build_safety_chain(
            self.config,
            interactive_approval=InteractiveApproval(prompt_fn),
            permissions_manager=self.permissions_manager,
            disposition_resolver=self.auto_approval_policy,
        )
        return ApprovalBridge(
            normalizer,
            sessions,
            approval=shared_approval,
            cwd=cwd,
            thread_id=thread_id,
        )


__all__ = ["ApprovalRuntimeManager"]
