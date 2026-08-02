"""Thread 子域共享错误类型。"""


class ThreadPresetRefreshError(RuntimeError):
    """thread preset 已回滚，因为新 runtime 刷新失败。"""


class ThreadForkConflictError(RuntimeError):
    """源 thread 仍有运行中或排队输入，当前快照尚未达到完整边界。"""


class ThreadPermissionsRevisionConflictError(RuntimeError):
    """thread permissions 的 REST revision 已落后于持久真值。"""

    def __init__(
        self,
        *,
        thread_id: str,
        expected_revision: int,
        actual_revision: int,
    ) -> None:
        """保存冲突坐标，供路由稳定映射为 HTTP 409。"""
        self.thread_id = thread_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            f"thread permissions revision conflict for {thread_id!r}: "
            f"expected={expected_revision}, actual={actual_revision}"
        )


class ThreadPermissionsValidationError(ValueError):
    """PUT 请求携带的 permissions DSL 不满足 canonical 合同。"""

    def __init__(self, *, thread_id: str, message: str) -> None:
        """保存目标 thread 与稳定的人读错误说明。"""
        self.thread_id = thread_id
        self.message = message
        super().__init__(message)


class ThreadPermissionsStorageError(RuntimeError):
    """thread permissions 持久化或 schema 迁移当前不可用。"""

    def __init__(self, *, thread_id: str, message: str) -> None:
        """保存目标 thread 与底层失败说明。"""
        self.thread_id = thread_id
        self.message = message
        super().__init__(message)
