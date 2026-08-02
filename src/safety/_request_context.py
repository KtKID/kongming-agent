"""Safety 内部统一解析 ApprovalRequest 的 cwd 语义。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.contracts import ApprovalRequest

_SHELL_TOOL_NAME = "run_shell"


@dataclass(frozen=True)
class SafetyRequestContext:
    """冻结 Safety 消费的 cwd 及其 execution scope 约束。"""

    cwd: str | None
    requires_execution_scope: bool

    @classmethod
    def from_request(cls, request: ApprovalRequest) -> SafetyRequestContext:
        """Shell 只读取 prepared scope，其他工具继续读取 runtime metadata。"""
        if request.tool_name == _SHELL_TOOL_NAME:
            raw_scope_cwd = request.execution_scope.cwd
            if (
                isinstance(raw_scope_cwd, str)
                and raw_scope_cwd.strip()
                and Path(raw_scope_cwd).is_absolute()
            ):
                return cls(cwd=raw_scope_cwd, requires_execution_scope=True)
            return cls(cwd=None, requires_execution_scope=True)

        raw_metadata_cwd = request.metadata.get("cwd")
        cwd = (
            raw_metadata_cwd
            if isinstance(raw_metadata_cwd, str) and raw_metadata_cwd.strip()
            else None
        )
        return cls(cwd=cwd, requires_execution_scope=False)

    @property
    def missing_required_cwd(self) -> bool:
        """返回 Shell 是否缺少合法 prepared execution scope。"""
        return self.requires_execution_scope and self.cwd is None

    def path_base(self) -> Path:
        """返回相对路径解析基准；缺少 Shell scope 时失败关闭。"""
        if self.cwd is not None:
            return Path(self.cwd).expanduser()
        if self.requires_execution_scope:
            raise ValueError("run_shell requires an absolute prepared execution_scope.cwd")
        return Path.cwd()


__all__ = ["SafetyRequestContext"]
