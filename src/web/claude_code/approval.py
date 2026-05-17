"""审批桥接（v0.1 + smart-approval-v1）。

把 SDK 的同步 ``can_use_tool`` 回调跟 WebSocket 异步 round-trip 桥接：

1. SDK 调 ``can_use_tool(tool_name, input, ctx)``
2. 检查 ``_allow_list`` —— 命中则直接 allow
3. **smart-approval-v1**：调 ``policy.classify`` → ``Decision``
4. emit ``permission_request`` 到当前 writer（带 v1 新字段 ``autoApproveAtMs`` / ``blockedByRule`` / ``channel``）
5. audit.log_request
6. 创建 ``asyncio.Future``，挂在 ``_pending[request_id]``；若 decision.auto_eligible → 用
   ``asyncio.wait_for(timeout)`` 等响应，超时自动 allow
7. allow → 返回 ``PermissionResultAllow``；deny → 通知 normalizer 去重 + 返回 ``PermissionResultDeny``
8. audit.log_decision

参考 ccui ``server/claude-sdk.js`` 第 530-600 行 ``canUseTool`` 实现。

设计要点：

- ``_active_writer``：service.query 调用前注入；can_use_tool 走这个 writer
  emit permission_request；service.query 退出后清空
- ``_active_cwd``：service.query 调用前注入（cwd 可能被 options 覆盖）；can_use_tool
  classify 时用
- ``_allow_list``：内存 list（thread 级生命周期），重启进程即丢；ccui 同款
- ``_matches`` 静态方法：精确名匹配 + ``Bash(prefix:*)`` 通配（不实现完整 glob）
- 智能审批为 None 时（``policy=None``）走 v0.1 老行为（兼容旧测试 + 未装配场景）
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from web._shared.session_manager import SessionManager
from web.auto_approval import AuditLogger, AutoApprovalPolicy
from web.claude_code.normalizer import ClaudeNormalizer

logger = logging.getLogger(__name__)

# 形如 ``Bash(npm:*)`` —— prefix 匹配 ccui 简化通配
_BASH_PERMISSION_RE = re.compile(r"^Bash\((.+):\*\)$")

# elevated 模式硬约束：本通道无 elevated 概念，永远 False
# （v0.1.6 elevated 是 kongming generic_chat 的概念；claude_code 通道
# 由 SDK + permission_mode 控制，所有进入 can_use_tool 的都是 standard 路径。
# 留 hook 是为了未来 claude_code 通道引入 elevated 时直接复用 policy 路径）
_IS_ELEVATED_FOR_CLAUDE_CHANNEL: bool = False


class ApprovalBridge:
    """审批桥接器（per-connection）。

    生命周期：每个 WebSocket 连接一个独立实例，避免跨连接的
    Future / allow_list 污染。

    smart-approval-v1 新增依赖（**可选**，None 时走老行为）：

    - ``policy``: ``AutoApprovalPolicy``——决策是否自动通过
    - ``audit``: ``AuditLogger``——审计落盘
    - ``cwd`` / ``thread_id``: 透传到 policy.classify + audit
    """

    def __init__(
        self,
        normalizer: ClaudeNormalizer,
        sessions: SessionManager,
        *,
        policy: AutoApprovalPolicy | None = None,
        audit: AuditLogger | None = None,
        cwd: str = "",
        thread_id: str | None = None,
        channel: str = "claude_code",
    ) -> None:
        self._normalizer = normalizer
        self._sessions = sessions
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._allow_list: list[str] = []
        self._active_writer: Any = None
        # smart-approval-v1
        self._policy = policy
        self._audit = audit
        self._active_cwd: str = cwd
        self._thread_id: str | None = thread_id
        self._channel: str = channel

    # ----- 公共接口 -----

    def set_active_writer(self, writer: Any) -> None:
        """注入当前 query 的 writer（service.query 调用前必须先调此）。"""
        self._active_writer = writer

    def clear_active_writer(self) -> None:
        """清空当前 writer（service.query 退出 finally 必须调此）。"""
        self._active_writer = None

    def set_active_cwd(self, cwd: str) -> None:
        """注入当前 query 的 cwd（options 可能覆盖默认 cwd，这里 service.query 内调）。"""
        if cwd:
            self._active_cwd = cwd

    @property
    def allow_list(self) -> list[str]:
        """暴露 allow_list 副本，单测断言用。"""
        return list(self._allow_list)

    @property
    def policy(self) -> AutoApprovalPolicy | None:
        """暴露 policy，单测 / 状态查询用。"""
        return self._policy

    @property
    def active_cwd(self) -> str:
        return self._active_cwd

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

        # === smart-approval-v1 决策 ===
        decision = (
            self._policy.classify(
                tool_name=tool_name,
                tool_input=tool_input,
                cwd=self._active_cwd,
                is_elevated=_IS_ELEVATED_FOR_CLAUDE_CHANNEL,
            )
            if self._policy is not None
            else None
        )

        # v1.0.1: 安全路径用 autoApproveAtMs，危险路径用 autoRejectAtMs，两者互斥
        # toggle OFF / elevated 时两者都 None（仍走 v0.1 无限等老路径）
        auto_approve_at_ms: int | None = None
        auto_reject_at_ms: int | None = None
        blocked_by_rule: str | None = None
        timeout_seconds: float | None = None
        if decision is not None:
            blocked_by_rule = decision.blocked_by_rule
            now_ms = int(time.time() * 1000)
            if decision.auto_eligible:
                # 安全路径：倒计时自动通过
                auto_approve_at_ms = now_ms + decision.timeout_ms
                timeout_seconds = decision.timeout_ms / 1000.0
            elif blocked_by_rule is not None:
                # v1.0.1 危险路径：倒计时自动拒绝（fail-closed）
                auto_reject_at_ms = now_ms + decision.timeout_ms
                timeout_seconds = decision.timeout_ms / 1000.0
            # else: toggle OFF / elevated → 两者都 None，timeout_seconds 也 None → 无限等

        permission_msg: dict[str, Any] = {
            "kind": "permission_request",
            "provider": "claude",
            "channel": self._channel,
            "requestId": request_id,
            "toolName": tool_name,
            "input": tool_input,
            "sessionId": session_id,
            "autoApproveAtMs": auto_approve_at_ms,
            "autoRejectAtMs": auto_reject_at_ms,
            "blockedByRule": blocked_by_rule,
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

        # === audit: log_request ===
        rule_eval = (
            decision.rule_evaluation if decision is not None else {"matched": None, "all_rules": []}
        )
        if self._audit is not None:
            try:
                self._audit.log_request(
                    channel=self._channel,
                    thread_id=self._thread_id,
                    cwd=self._active_cwd,
                    tool_name=tool_name,
                    arguments=tool_input,
                    matched_rule=rule_eval.get("matched"),
                    all_enabled_rules=list(rule_eval.get("all_rules", [])),
                    timeout_ms=decision.timeout_ms if decision is not None else 0,
                )
            except Exception:
                logger.exception("audit.log_request failed (non-fatal)")

        # 3. 创建 Future + 等响应
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future

        # v1.0.1: 区分 timeout 后是 auto-allow 还是 auto-reject
        auto_allowed_by_timeout = False
        auto_rejected_by_timeout = False
        try:
            if timeout_seconds is not None:
                # 智能审批：到点自动决策（不抛 TimeoutError 给上层）
                try:
                    user_decision = await asyncio.wait_for(future, timeout=timeout_seconds)
                except TimeoutError:
                    self._pending.pop(request_id, None)  # 防止后续 resolve 走错路
                    if blocked_by_rule is not None:
                        # v1.0.1 危险路径：自动拒绝（fail-closed）
                        auto_rejected_by_timeout = True
                        user_decision = {
                            "allow": False,
                            "auto_rejected": True,
                            "message": f"auto-rejected by rule {blocked_by_rule} after {decision.timeout_ms}ms timeout",
                        }
                    else:
                        # 安全路径：自动通过
                        auto_allowed_by_timeout = True
                        user_decision = {"allow": True, "auto": True}
            else:
                # 老行为：无限等（toggle OFF / elevated / policy=None）
                user_decision = await future
        except asyncio.CancelledError:
            # 中断（例如 abort）：丢掉 pending 后向上传
            self._pending.pop(request_id, None)
            self._log_decision(
                tool_name=tool_name,
                tool_input=tool_input,
                rule_eval=rule_eval,
                outcome="denied",
                decided_by=None,
                timeout_ms=decision.timeout_ms if decision is not None else 0,
            )
            raise
        finally:
            self._pending.pop(request_id, None)

        # 4. 处理 decision
        if user_decision.get("allow"):
            remember = user_decision.get("rememberEntry")
            if isinstance(remember, str) and remember and remember not in self._allow_list:
                self._allow_list.append(remember)

            outcome = "allowed_auto_timeout" if auto_allowed_by_timeout else "allowed_manual"
            decided_by = "timeout" if auto_allowed_by_timeout else "user"
            self._log_decision(
                tool_name=tool_name,
                tool_input=tool_input,
                rule_eval=rule_eval,
                outcome=outcome,
                decided_by=decided_by,
                timeout_ms=decision.timeout_ms if decision is not None else 0,
            )

            updated_input = user_decision.get("updatedInput")
            return PermissionResultAllow(
                updated_input=updated_input if updated_input is not None else tool_input,
            )

        # deny 路径：通知 normalizer 去重，返回 deny
        self._normalizer.add_pending_deny(request_id)
        # 三态 outcome：
        # - rejected_auto_timeout (v1.0.1)：命中规则 + 倒计时到点
        # - blocked_then_manual：命中规则 + 用户主动拒
        # - denied：未命中规则 + 用户主动拒
        if auto_rejected_by_timeout:
            outcome = "rejected_auto_timeout"
            decided_by: str | None = "timeout"
        elif blocked_by_rule:
            outcome = "blocked_then_manual"
            decided_by = "user"
        else:
            outcome = "denied"
            decided_by = "user"
        self._log_decision(
            tool_name=tool_name,
            tool_input=tool_input,
            rule_eval=rule_eval,
            outcome=outcome,
            decided_by=decided_by,
            timeout_ms=decision.timeout_ms if decision is not None else 0,
        )
        message = user_decision.get("message") or "User denied tool use"
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

    def _log_decision(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        rule_eval: dict[str, Any],
        outcome: str,
        decided_by: str | None,
        timeout_ms: int,
    ) -> None:
        """audit.log_decision 包装：吞异常，永不影响主流程。"""
        if self._audit is None:
            return
        try:
            self._audit.log_decision(
                channel=self._channel,
                thread_id=self._thread_id,
                cwd=self._active_cwd,
                tool_name=tool_name,
                arguments=tool_input,
                matched_rule=rule_eval.get("matched"),
                all_enabled_rules=list(rule_eval.get("all_rules", [])),
                outcome=outcome,
                decided_by=decided_by,
                timeout_ms=timeout_ms,
            )
        except Exception:
            logger.exception("audit.log_decision failed (non-fatal)")

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
