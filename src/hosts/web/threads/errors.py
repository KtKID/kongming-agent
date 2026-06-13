"""Thread 子域共享错误类型。"""


class ThreadPresetRefreshError(RuntimeError):
    """thread preset 已回滚，因为新 runtime 刷新失败。"""
