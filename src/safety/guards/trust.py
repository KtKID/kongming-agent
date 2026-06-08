"""safety v0.1.4 M6 — TrustResolver。

消费已有信任证据（intrinsic / session / config），命中即返回静默放行的
:class:`ApprovalDecision`；无证据时返回 ``None`` 让 ConsentResolver 接管。

链路位置（``decision_engine`` 装配顺序）：

    HardBlock → BoundaryResolver → **TrustResolver** → ConsentResolver

设计要点：

- **不创建 grant**：TrustResolver 只查询，新授权统一由 ``ApprovalAction.ACCEPT_FOR_SESSION``
  / ``ACCEPT_PERSIST`` 经 InteractiveApproval 路径在 ConsentResolver 完成。
- **HardBlock 不可被覆盖**：依赖装配顺序保证（HardBlock 在 TrustResolver 之前）。
- **运行时 boundary_kind 恒为 host**：所有 GrantKey 查询强制 ``BoundaryKind.HOST``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.contracts import ApprovalDecision, ApprovalRequest
from safety.approval.types import (
    ApprovalMetadataKeys,
    BoundaryKind,
    BoundaryZone,
    Grant,
    RuntimeBoundaryContext,
)

if TYPE_CHECKING:
    from safety.boundaries.resolver import BoundaryResolver
    from safety.grants.store import GrantStore

# 工具名 → capability 映射（与 GrantStore.from_config 写入一致）
_FILE_READ_TOOLS: frozenset[str] = frozenset({"read_file", "list_dir"})
_FILE_WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file"})
_SHELL_TOOLS: frozenset[str] = frozenset({"run_shell"})


class TrustResolver:
    """v0.1.4 信任证据消费层。

    返回 ``None`` 表示"无证据，让下层继续"；返回 ``ApprovalDecision`` 表示
    命中证据，直接静默放行。
    """

    def __init__(
        self,
        boundary_resolver: BoundaryResolver,
        grant_store: GrantStore,
    ) -> None:
        self._boundary = boundary_resolver
        self._grants = grant_store

    def evaluate(
        self,
        request: ApprovalRequest,
        runtime: RuntimeBoundaryContext,
    ) -> ApprovalDecision | None:
        """按 intrinsic → session → config 顺序查证据。

        命中即返回静默放行的 ``ApprovalDecision``；都不命中返回 ``None``。
        """
        # ---- intrinsic：BoundaryResolver 命中 trusted zone ----
        boundary = self._boundary.resolve(request, runtime)
        if boundary.zone is BoundaryZone.TRUSTED:
            return _silent_allow(
                source="intrinsic",
                matched_rule=boundary.matched_rule or "trusted_zone",
                reason=boundary.reason or "in trusted zone",
                grant_scope=None,
            )

        # ---- session / config：查 GrantStore ----
        grant = self._lookup_grant(request)
        if grant is not None:
            scope = grant.scope  # "session" | "config"
            return _silent_allow(
                source=scope,  # session/config 与 DecisionSource 同字面值
                matched_rule=_grant_rule_label(grant),
                reason=f"grant matched ({scope})",
                grant_scope=scope,
            )

        return None

    # ------------------------------------------------------------------
    # 内部 helper
    # ------------------------------------------------------------------

    def _lookup_grant(self, request: ApprovalRequest) -> Grant | None:
        """从 ApprovalRequest 派生 (capability, path_or_action) 查 GrantStore。

        覆盖三种 capability 形态：
        - ``file_read`` / ``file_write``：path 类工具
        - ``tool:<name>``：allow_tools_silent 通配
        - ``shell``：command 类（path_or_action 为命令首段）
        """
        capability, path_or_action = _derive_capability_and_target(request)
        if capability is None or path_or_action is None:
            return None

        # 1. 精确 capability 查询（session_id 严格隔离：仅查本 session 桶 + config）
        result = self._grants.find_matching(
            capability=capability,
            path_or_action=path_or_action,
            boundary_kind=BoundaryKind.HOST,
            session_id=request.session_id,
        )
        if result is not None:
            return result

        # 2. tool: 精确命令前缀匹配（session grant 细化后不再通配）
        tool_name = request.tool_name
        if tool_name:
            tool_capability = f"tool:{tool_name}"

            # 2a. shell 命令：用命令前缀查 grant（如 "ls"、"git"）
            if tool_name in _SHELL_TOOLS:
                cmd = (request.arguments or {}).get("command")
                if isinstance(cmd, str) and cmd.strip():
                    from safety.approval.chain import extract_command_base

                    cmd_base = extract_command_base(cmd)
                    if cmd_base:
                        result = self._grants.find_matching(
                            capability=tool_capability,
                            path_or_action=cmd_base,
                            boundary_kind=BoundaryKind.HOST,
                            session_id=request.session_id,
                        )
                        if result is not None:
                            return result

            # 2b. 通配 matcher="*"（allow_tools_silent / 非 shell 工具的 session grant）
            result = self._grants.find_matching(
                capability=tool_capability,
                path_or_action="*",
                boundary_kind=BoundaryKind.HOST,
                session_id=request.session_id,
            )
            if result is not None:
                return result

        return None


# ---------------------------------------------------------------------------
# 模块级 helper
# ---------------------------------------------------------------------------


def _derive_capability_and_target(
    request: ApprovalRequest,
) -> tuple[str | None, str | None]:
    """从 ApprovalRequest 推导 (capability, path_or_action) 二元组。"""
    name = request.tool_name
    args: dict[str, Any] = dict(request.arguments or {})

    if name in _FILE_READ_TOOLS:
        path = args.get("path")
        if isinstance(path, str):
            return "file_read", path
    if name in _FILE_WRITE_TOOLS:
        path = args.get("path")
        if isinstance(path, str):
            return "file_write", path
    if name in _SHELL_TOOLS:
        cmd = args.get("command")
        if isinstance(cmd, str) and cmd.strip():
            # 命令类 grant 的 path_or_action 是 action profile（如 "git_push"）
            # 或命令首段。本次实现：取首 token 作为 action 标识
            first_token = cmd.strip().split(maxsplit=1)[0]
            return "shell", first_token

    return None, None


def _grant_rule_label(grant: Grant) -> str:
    """把 Grant 转成 trace 友好的规则标识。"""
    return f"grant:{grant.key.capability}:{grant.key.matcher}"


def _silent_allow(
    *,
    source: str,
    matched_rule: str,
    reason: str,
    grant_scope: str | None,
) -> ApprovalDecision:
    """构造 silent_allow ApprovalDecision。

    metadata 严格按 04 文档约定填充：``decision_class`` / ``decision_source`` /
    ``matched_rule`` / ``reason`` / ``boundary_kind`` 必填；``grant_scope`` 仅
    session/config 携带（intrinsic 不写）。
    """
    metadata: dict[str, Any] = {
        ApprovalMetadataKeys.DECISION_CLASS: "silent_allow",
        ApprovalMetadataKeys.DECISION_SOURCE: source,
        ApprovalMetadataKeys.MATCHED_RULE: matched_rule,
        ApprovalMetadataKeys.REASON: reason,
        ApprovalMetadataKeys.BOUNDARY_KIND: "host",
    }
    if grant_scope is not None:
        metadata[ApprovalMetadataKeys.GRANT_SCOPE] = grant_scope

    return ApprovalDecision(
        outcome="approved",
        reason=f"[silent_allow:{source}] {reason}",
        metadata=metadata,
    )
