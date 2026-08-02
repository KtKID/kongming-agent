"""thread permissions 门户对外暴露的稳定错误语义。"""

from __future__ import annotations


class PermissionsError(Exception):
    """thread permissions 操作失败的基类。"""


class PermissionsDataError(PermissionsError):
    """持久文件或待写表达式违反 permissions 数据合同。"""


class PermissionsExpressionError(PermissionsDataError):
    """调用方提交的 DSL 表达式非法或未 canonicalize。"""


class PermissionsRevisionConflict(PermissionsError):
    """调用方提交的 revision 已落后于磁盘真值。"""

    def __init__(self, *, thread_id: str, expected_revision: int, actual_revision: int) -> None:
        """记录冲突 thread 与期望、实际 revision。"""
        self.thread_id = thread_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            f"permissions revision conflict for {thread_id!r}: "
            f"expected={expected_revision}, actual={actual_revision}"
        )


class PermissionsStoreError(PermissionsError):
    """permissions 文件系统读写失败。"""


__all__ = [
    "PermissionsDataError",
    "PermissionsError",
    "PermissionsExpressionError",
    "PermissionsRevisionConflict",
    "PermissionsStoreError",
]
