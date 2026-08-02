"""Scoped permission support for in-process sub-agents."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from core.contracts import (
    PreparedToolCall,
    Session,
    Tool,
    ToolCallPreparer,
    ToolContext,
    ToolResult,
)
from core.errors import ToolPreparationError
from core.message import Message, ToolCall
from core.result import Result
from core.run_state import RunState

SCOPED_WORKDIR_MODE = "scoped_workdir"
SUBAGENT_APPROVAL_ACTION = "subagent_approval_decided"
ALLOWED_SCOPED_FILE_TOOLS = frozenset({"read_file", "write_file", "list_dir"})
_FILE_TOOL_PATH_FIELD = {"read_file": "path", "write_file": "path", "list_dir": "path"}


@dataclass(frozen=True)
class SubAgentPermissionSpec:
    # 权限模式，第一版只支持 scoped_workdir。
    mode: Literal["scoped_workdir"]


@dataclass(frozen=True)
class SubAgentGrant:
    # 授权单唯一 ID，写入每条审批审计。
    grant_id: str
    # workflow ID，用于定位审计目录。
    workflow_id: str
    # 父 session ID，用于串联主 agent 会话。
    parent_session_id: str
    # 子任务原始 ID，用于保留主 agent 分派语义。
    task_id: str
    # 子任务运行 ID，用于定位 agents/<task_run_id>/。
    task_run_id: str
    # 子任务名称，用于展示和审计。
    task_name: str
    # 子 agent session ID，用于关联独立会话日志。
    session_id: str
    # 子任务运行目录绝对路径，包含 subagent.json 和 work/。
    task_run_dir: Path
    # 子 agent 专属工作目录绝对路径，固定为 agents/<task_run_id>/work/。
    working_dir: Path
    # workflow 审计目录绝对路径。
    workflow_dir: Path
    # 允许执行的工具名集合。
    allowed_tools: frozenset[str]
    # 允许记录的 skill 名集合。第一版仅用于审计和创建快照。
    allowed_skills: frozenset[str]
    # 授权创建时间，ISO 8601 字符串。
    created_at: str


@dataclass(frozen=True)
class SubAgentCreationRecord:
    # 创建记录版本号。
    version: int
    # workflow ID。
    workflow_id: str
    # 子任务运行 ID。
    task_run_id: str
    # 子 agent session ID。
    session_id: str
    # 子任务原始 ID。
    task_id: str
    # 子任务原始名称。
    task_name: str
    # 子任务原始 prompt。
    prompt: str
    # 派发给子 agent 的最小上下文文本。
    context: str
    # 主 agent 选择的工具名。
    tool_names: tuple[str, ...]
    # 主 agent 选择的 skill 名。
    skill_names: tuple[str, ...]
    # 子 agent 解析后的运行参数快照。
    resolved_runtime: dict[str, Any]
    # 权限输入结构。
    permission: SubAgentPermissionSpec
    # 子 agent 最终授权单。
    grant: SubAgentGrant
    # 子任务运行目录。
    task_run_dir: Path
    # 子 agent 专属工作目录。
    working_dir: Path
    # 子 agent 会话日志路径。
    child_session_log_path: Path
    # workflow 审计日志路径。
    workflow_audit_log_path: Path
    # 创建时间，ISO 8601 字符串。
    created_at: str


@dataclass(frozen=True)
class SubAgentApprovalAuditPayload:
    # 审计事件类型，固定为 subagent_approval_decided。
    action: Literal["subagent_approval_decided"]
    # workflow ID。
    workflow_id: str
    # 子任务运行 ID。
    task_run_id: str
    # 子 agent session ID。
    session_id: str
    # 授权单 ID。
    grant_id: str
    # 工具调用 ID。
    tool_call_id: str
    # 工具名。
    tool_name: str
    # 原始工具参数。
    raw_args: dict[str, Any]
    # 工具参数 sha256 摘要。
    raw_args_digest: str
    # 原始 path 参数。
    target_path: str | None
    # 规范化后的目标路径，无法解析时为空。
    resolved_path: str | None
    # 子 agent 工作目录。
    working_dir: str
    # 审批结果，approved 或 rejected。
    decision: Literal["approved", "rejected"]
    # 决策来源，例如 grant_allow、scope_deny、tool_deny、not_registered、missing_path。
    decision_source: str
    # 审批原因。
    reason: str
    # 当前 run ID。
    run_id: str
    # 当前 turn。
    turn: int
    # 事件时间，ISO 8601 字符串。
    created_at: str


class WorkflowAuditWriter(Protocol):
    @property
    def audit_log_path(self) -> Path:
        # workflow audit.jsonl 路径。
        ...

    def write_event(self, event: Mapping[str, Any]) -> None:
        # 写入 workflow audit.jsonl 的唯一入口。
        ...

    def write_subagent_creation(self, record: SubAgentCreationRecord) -> None:
        # 写入 agents/<task_run_id>/subagent.json 的唯一入口。
        ...


@dataclass(frozen=True)
class _ScopedPathDecision:
    # 原始 path 参数。
    target_path: str | None
    # resolve 后的绝对路径。
    resolved_path: Path | None
    # 是否位于 working_dir 内。
    allowed: bool
    # 决策来源。
    decision_source: str
    # 人类可读原因。
    reason: str


class ScopedFileTool:
    """Tool wrapper that binds file tool paths to one sub-agent working dir."""

    def __init__(self, *, inner_tool: Tool, grant: SubAgentGrant) -> None:
        self._inner_tool = inner_tool
        self._grant = grant
        self.name = inner_tool.name
        self.description = inner_tool.description
        self.input_schema = inner_tool.input_schema

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前冻结 scoped path，并继续调用内部工具唯一 prepare。"""
        path_decision = _resolve_scoped_path(
            tool_name=self.name,
            args=arguments,
            working_dir=self._grant.working_dir,
        )
        if not path_decision.allowed or path_decision.resolved_path is None:
            raise ToolPreparationError(
                path_decision.reason,
                details={
                    "decision_source": path_decision.decision_source,
                    "working_dir": str(self._grant.working_dir),
                    "target_path": path_decision.target_path,
                    "resolved_path": (
                        str(path_decision.resolved_path)
                        if path_decision.resolved_path is not None
                        else None
                    ),
                },
            )
        scoped_args = dict(arguments)
        scoped_args[_FILE_TOOL_PATH_FIELD[self.name]] = str(path_decision.resolved_path)
        if isinstance(self._inner_tool, ToolCallPreparer):
            return self._inner_tool.prepare(scoped_args, context)
        return PreparedToolCall(arguments=scoped_args)

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        """把同一 prepared 快照交给内部工具执行。"""
        return await self._inner_tool.execute(prepared, ctx)


class SubAgentToolAuditHook:
    """Lifecycle hook that audits child tool calls skipped before approval."""

    def __init__(self, *, grant: SubAgentGrant, audit_writer: WorkflowAuditWriter) -> None:
        self._grant = grant
        self._audit_writer = audit_writer
        self._calls: dict[str, ToolCall] = {}

    async def before_turn(self, state: RunState) -> None:
        return None

    async def after_turn(self, state: RunState, assistant_message: Message) -> None:
        return None

    async def before_tool(self, state: RunState, call: ToolCall) -> None:
        self._calls[call.call_id] = call

    async def after_tool(self, state: RunState, call: ToolCall, result_message: Message) -> None:
        error_message = result_message.metadata.get("error_message")
        if not isinstance(error_message, str):
            return
        if error_message != f"tool {call.tool_name!r} not registered":
            return
        raw_call = self._calls.get(call.call_id, call)
        payload = build_approval_audit_payload(
            grant=self._grant,
            run_id=state.run_id,
            turn=state.turn,
            call_id=raw_call.call_id,
            tool_name=raw_call.tool_name,
            raw_args=raw_call.arguments,
            decision="rejected",
            decision_source="not_registered",
            reason=error_message,
        )
        write_approval_audit(self._audit_writer, payload)

    async def after_run(self, state: RunState, session: Session, result: Result) -> None:
        return None


def parse_permission_spec(raw: object) -> SubAgentPermissionSpec:
    if not isinstance(raw, Mapping):
        raise ValueError("permission must be an object")
    mode = raw.get("mode")
    if mode != SCOPED_WORKDIR_MODE:
        raise ValueError("permission.mode must be scoped_workdir")
    return SubAgentPermissionSpec(mode="scoped_workdir")


def validate_scoped_tool_names(tool_names: tuple[str, ...]) -> None:
    invalid = sorted(set(tool_names) - ALLOWED_SCOPED_FILE_TOOLS)
    if invalid:
        raise ValueError(f"scoped_workdir only supports file tools: {invalid}")


def wrap_scoped_file_tools(tools: list[Tool], grant: SubAgentGrant) -> list[Tool]:
    return [
        ScopedFileTool(inner_tool=tool, grant=grant)
        if tool.name in ALLOWED_SCOPED_FILE_TOOLS
        else tool
        for tool in tools
    ]


def build_approval_audit_payload(
    *,
    grant: SubAgentGrant,
    run_id: str,
    turn: int,
    call_id: str,
    tool_name: str,
    raw_args: dict[str, Any],
    decision: Literal["approved", "rejected"],
    decision_source: str,
    reason: str,
    path_decision: _ScopedPathDecision | None = None,
) -> SubAgentApprovalAuditPayload:
    raw_args_copy = dict(raw_args)
    target_path = (
        path_decision.target_path if path_decision is not None else _raw_path(raw_args_copy)
    )
    resolved_path = (
        str(path_decision.resolved_path)
        if path_decision is not None and path_decision.resolved_path is not None
        else None
    )
    return SubAgentApprovalAuditPayload(
        action="subagent_approval_decided",
        workflow_id=grant.workflow_id,
        task_run_id=grant.task_run_id,
        session_id=grant.session_id,
        grant_id=grant.grant_id,
        tool_call_id=call_id,
        tool_name=tool_name,
        raw_args=raw_args_copy,
        raw_args_digest=_digest(raw_args_copy),
        target_path=target_path,
        resolved_path=resolved_path,
        working_dir=str(grant.working_dir),
        decision=decision,
        decision_source=decision_source,
        reason=reason,
        run_id=run_id,
        turn=turn,
        created_at=_now_iso(),
    )


def write_approval_audit(
    audit_writer: WorkflowAuditWriter,
    payload: SubAgentApprovalAuditPayload,
) -> None:
    audit_writer.write_event(
        {
            "action": payload.action,
            "payload": to_jsonable(payload),
        }
    )


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(val) for key, val in value.items()}
    if isinstance(value, tuple | list | set | frozenset):
        return [to_jsonable(item) for item in value]
    return value


def _resolve_scoped_path(
    *,
    tool_name: str,
    args: Mapping[str, Any],
    working_dir: Path,
) -> _ScopedPathDecision:
    path_field = _FILE_TOOL_PATH_FIELD.get(tool_name)
    if path_field is None:
        return _ScopedPathDecision(
            target_path=None,
            resolved_path=None,
            allowed=False,
            decision_source="tool_deny",
            reason=f"tool {tool_name!r} is not a scoped file tool",
        )
    raw = args.get(path_field)
    if not isinstance(raw, str) or not raw:
        return _ScopedPathDecision(
            target_path=None,
            resolved_path=None,
            allowed=False,
            decision_source="missing_path",
            reason=f"tool {tool_name!r} requires non-empty {path_field!r}",
        )

    try:
        base = working_dir.expanduser().resolve()
        raw_path = Path(raw).expanduser()
        candidate = raw_path if raw_path.is_absolute() else base / raw_path
        resolved = candidate.resolve()
    except OSError as exc:
        return _ScopedPathDecision(
            target_path=raw,
            resolved_path=None,
            allowed=False,
            decision_source="invalid_path",
            reason=f"failed to resolve path {raw!r}: {exc}",
        )

    if not _is_relative_to(resolved, base):
        return _ScopedPathDecision(
            target_path=raw,
            resolved_path=resolved,
            allowed=False,
            decision_source="scope_deny",
            reason=f"target path is outside subagent working_dir: {resolved}",
        )
    return _ScopedPathDecision(
        target_path=raw,
        resolved_path=resolved,
        allowed=True,
        decision_source="grant_allow",
        reason="target path is inside subagent working_dir",
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _raw_path(args: Mapping[str, Any]) -> str | None:
    value = args.get("path")
    return value if isinstance(value, str) else None


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "ALLOWED_SCOPED_FILE_TOOLS",
    "SCOPED_WORKDIR_MODE",
    "SUBAGENT_APPROVAL_ACTION",
    "ScopedFileTool",
    "SubAgentApprovalAuditPayload",
    "SubAgentCreationRecord",
    "SubAgentGrant",
    "SubAgentPermissionSpec",
    "SubAgentToolAuditHook",
    "WorkflowAuditWriter",
    "parse_permission_spec",
    "to_jsonable",
    "validate_scoped_tool_names",
    "wrap_scoped_file_tools",
]
