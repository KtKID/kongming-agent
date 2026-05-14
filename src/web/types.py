"""Web 装配层用到的 typing.Protocol 接口。

主要用于：

- **测试 mock**：路由层 / WS 层在单测里注入 :class:`ThreadManagerProtocol`
  实现，免装配真实 :class:`web.thread_manager.ThreadManager`。
- **类型注解**：路由 handler 用 Protocol 而非具体类型，便于将来替换实现。

生产代码仍 import 真实类（``from web.thread_manager import ThreadManager``）；
本文件只是接口面契约，不负责生产实现。

Why typing.Protocol：

- 与具体类完全解耦（无需继承）
- ``@runtime_checkable`` 支持 ``isinstance(obj, ThreadManagerProtocol)``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from web.protocol import CellSummaryDTO
    from web.thread_metadata import ThreadMetadata


@runtime_checkable
class ThreadCellProtocol(Protocol):
    """ThreadCell 接口的最小子集（仅 web-app-shell 路由 / WS 需要的部分）。"""

    thread_id: str
    metadata: Any  # ThreadMetadata
    adapter: Any  # WebHostAdapter
    bridge: Any  # SessionBridge

    def attach_ws(self, new_ws: Any) -> None: ...

    def detach_ws(self, ws: Any) -> None: ...

    def touch(self) -> None: ...

    @property
    def has_pending_approvals(self) -> bool: ...


@runtime_checkable
class ThreadManagerProtocol(Protocol):
    """ThreadManager 接口的最小子集。

    路由层 / WS 层依赖此 Protocol；测试可用 :class:`unittest.mock.AsyncMock`
    或自写 fake 实现满足。
    """

    @property
    def started(self) -> bool: ...

    @property
    def closed(self) -> bool: ...

    async def start(self) -> None: ...

    async def aclose_all(self) -> None: ...

    # CRUD
    async def create_thread(
        self,
        name: str,
        preset_id: str = "",
        *,
        backend_kind: Literal["generic_chat", "claude_code", "codex"] = "generic_chat",
        cwd: str = "",
    ) -> ThreadMetadata: ...

    async def rename_thread(self, thread_id: str, new_name: str) -> ThreadMetadata: ...

    async def pin_thread(self, thread_id: str, is_pinned: bool) -> ThreadMetadata: ...

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None: ...

    async def boot_or_attach(self, thread_id: str) -> ThreadCellProtocol: ...

    async def evict_cell(
        self,
        thread_id: str,
        reason: str,
        message: str | None = None,
        *,
        notify_ws: bool = True,
    ) -> None: ...

    # 查询（同步）
    def list_threads(self) -> list[ThreadMetadata]: ...

    def list_cells(self) -> list[CellSummaryDTO]: ...

    def get_cell(self, thread_id: str) -> ThreadCellProtocol | None: ...

    def find_thread_by_claude_thread_id(self, claude_thread_id: str) -> ThreadMetadata | None: ...

    def find_thread_by_codex_thread_id(self, codex_thread_id: str) -> ThreadMetadata | None: ...

    async def bind_claude_thread(
        self,
        thread_id: str,
        claude_thread_id: str,
        cwd: str,
    ) -> ThreadMetadata: ...

    async def bind_codex_thread(
        self,
        thread_id: str,
        codex_thread_id: str,
        cwd: str,
    ) -> ThreadMetadata: ...

    # task#3.3：移除 add_thread_usage —— UsagePersistSink 改走
    # ``usage_manager.record_run_usage``；router / WS 处理器通过 ``usage_manager``
    # 属性拿 ``UsageTokenManager`` 实例。

    @property
    def usage_manager(self) -> Any:
        """:class:`web.usage_token.UsageTokenManager` 实例（task#3.3 注入）。

        ``Any`` 而非具体类型——避免 ``web.types`` Protocol 文件 import
        ``web.usage_token``（保持 types.py 零运行时依赖）。具体类型由
        ``web.thread_manager.ThreadManager.usage_manager`` 提供。
        """
        ...

    def resolve_approval(self, thread_id: str, call_id: str, approved: bool) -> None: ...


__all__ = [
    "ThreadCellProtocol",
    "ThreadManagerProtocol",
]
