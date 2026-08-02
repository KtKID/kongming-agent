"""Web 装配层用到的 typing.Protocol 接口。

主要用于：

- **测试 mock**：路由层 / WS 层在单测里注入 :class:`ThreadManagerProtocol`
  实现，免装配真实 :class:`web.threads.manager.ThreadManager`。
- **类型注解**：路由 handler 用 Protocol 而非具体类型，便于将来替换实现。

生产代码仍 import 真实类（``from hosts.web.threads.manager import ThreadManager``）；
本文件只是接口面契约，不负责生产实现。

Why typing.Protocol：

- 与具体类完全解耦（无需继承）
- ``@runtime_checkable`` 支持 ``isinstance(obj, ThreadManagerProtocol)``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from hosts.web.protocol import (
        CellSummaryDTO,
        EvictReason,
        PermissionRuleDTO,
        ThreadPermissionsDTO,
    )
    from hosts.web.threads.metadata import ThreadMetadata


@runtime_checkable
class ThreadPermissionsManagerProtocol(Protocol):
    """Web REST 层读取和整本替换 thread permissions 的稳定门户。"""

    async def get(self, thread_id: str) -> ThreadPermissionsDTO:
        """读取指定 thread 的 REST 快照。"""
        ...

    async def replace(
        self,
        thread_id: str,
        *,
        allow: list[PermissionRuleDTO],
        deny: list[PermissionRuleDTO],
        expected_revision: int,
    ) -> ThreadPermissionsDTO:
        """按 revision CAS 整本替换并返回新 REST 快照。"""
        ...


@runtime_checkable
class ThreadCellProtocol(Protocol):
    """ThreadCell 接口的最小子集（仅 web-app-shell 路由 / WS 需要的部分）。"""

    thread_id: str
    metadata: Any  # ThreadMetadata
    runtime: Any  # SessionEngine
    adapter: Any  # WebHostAdapter
    host_dispatcher: Any  # HostDispatcher

    def attach_ws(self, new_ws: Any) -> None:
        """把新 WebSocket 连接挂到 cell 的 adapter 和事件 sink 上。"""
        ...

    def detach_ws(self, ws: Any) -> None:
        """注销断开的 WebSocket；只影响该连接，不关闭 thread runtime。"""
        ...

    def get_client_event_sink(self) -> Any | None:
        """返回面向浏览器的事件 sink，供同宿主内的旁路事件复用。"""
        ...

    def touch(self) -> None:
        """刷新最近活动时间，供 idle eviction 和连接保活判断使用。"""
        ...


@runtime_checkable
class ThreadManagerProtocol(Protocol):
    """ThreadManager 接口的最小子集。

    路由层 / WS 层依赖此 Protocol；测试可用 :class:`unittest.mock.AsyncMock`
    或自写 fake 实现满足。

    pending input 相关 submit 入口是普通输入、ChoicePanel 和 Avatar 的统一
    run gate：实现层负责在 idle 时立即启动 run，在 active run 期间写入队列。
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

    async def create_scheduled_task_thread(
        self,
        *,
        task_id: str,
        name: str,
        preset_id: str,
        cwd: str = "",
    ) -> str: ...

    async def create_generic_thread_from_first_message(
        self,
        *,
        text: str,
        preset_id: str,
        cwd: str = "",
        reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = None,
    ) -> ThreadMetadata: ...

    async def fork_thread(
        self,
        source_thread_id: str,
        *,
        history_index: int | None = None,
    ) -> ThreadMetadata:
        """复制 generic chat 历史前缀并创建带 lineage 的新 thread。"""
        ...

    async def rename_thread(self, thread_id: str, new_name: str) -> ThreadMetadata: ...

    async def pin_thread(self, thread_id: str, is_pinned: bool) -> ThreadMetadata: ...

    async def set_archived(self, thread_id: str, is_archived: bool) -> ThreadMetadata: ...

    async def update_thread_preset(self, thread_id: str, preset_id: str) -> ThreadMetadata: ...

    async def ensure_cell_runtime_preset_current(self, thread_id: str) -> bool:
        """确保活跃 cell 的 runtime preset 已追上 metadata；返回是否可继续提交输入。"""
        ...

    async def submit_user_input(
        self,
        thread_id: str,
        text: str,
        *,
        request_id: str | None = None,
        reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        source_conn_id: str | None = None,
    ) -> Any:
        """提交普通用户输入；idle 直接启动 run，running 写入 pending input 队列。

        关键输入：thread_id、用户原文 text，以及 request_id、reasoning_effort、
        attachments、references、source_conn_id 等透传参数。
        关键输出：提交结果对象；调用方通过 started 判断本次输入已启动或已入队。
        """
        ...

    async def submit_choice_result(
        self,
        thread_id: str,
        choice_text: str,
        *,
        request_id: str,
    ) -> Any:
        """提交 choice 结果；实现层以更高优先级入队，保证选择回复优先 drain。

        关键输入：格式化后的 choice_text 与 request_id。
        关键输出：与普通输入一致的提交结果；choice_response 优先级由实现层落地。
        """
        ...

    async def submit_avatar_input(
        self,
        thread_id: str,
        text: str,
        *,
        request_id: str | None = None,
        reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        avatar_run_id: str | None = None,
    ) -> Any:
        """提交 Avatar 输入；复用普通 pending queue，并透传 Avatar 运行关联字段。

        关键输入：thread_id、Avatar 生成的文本、附件和 avatar_run_id。
        关键输出：提交结果对象；avatar_run_id 用于前端关联队列启动事件。
        """
        ...

    async def cancel_pending_input(self, thread_id: str, pending_input_id: str) -> Any:
        """删除尚未启动的排队输入；返回删除后的队列快照。

        关键输入：thread_id 和 pending_input_id。
        关键输出：服务端队列真源快照；运行中的 current_run_task 由实现层继续托管。
        """
        ...

    async def update_pending_input(
        self,
        thread_id: str,
        pending_input_id: str,
        content: str,
    ) -> Any:
        """更新尚未启动的排队输入内容；空内容由实现层拒绝。

        调用方只提交新 content；实现层负责 trim、版本递增和 WS changed 广播。
        """
        ...

    async def reorder_pending_inputs(
        self,
        thread_id: str,
        ordered_ids: list[str],
    ) -> Any:
        """按浏览器拖拽后的最终顺序重排尚未启动的队列项。

        关键输入：ordered_ids 必须覆盖当前队列内全部 pending item。
        关键输出：服务端队列真源快照；非法 ID、缺失或重复由实现层拒绝。
        """
        ...

    async def send_pending_input_now(
        self,
        thread_id: str,
        pending_input_id: str,
    ) -> Any:
        """立即发送尚未启动的排队输入。

        实现层负责 active run 插入或 idle 启动；结构化输入不可插入时保留队列项。
        """
        ...

    async def pending_input_snapshot(self, thread_id: str) -> Any:
        """读取当前排队输入快照；用于 WS 连接建立后的状态同步。

        快照包含 items、max_items、active_run_id 和 version，前端 reducer 以它为真源。
        """
        ...

    async def interrupt_agent_tree(
        self,
        thread_id: str,
        *,
        reason: str = "user_interrupt",
    ) -> bool:
        """打断当前 root agent 树；返回是否存在活跃工作并已发起打断。"""
        ...

    async def delete_thread(self, thread_id: str, *, keep_history: bool = False) -> None: ...

    async def boot_or_attach(self, thread_id: str) -> ThreadCellProtocol: ...

    async def build_ephemeral_session_cell(
        self,
        *,
        session_id: str,
        preset_id: str,
    ) -> ThreadCellProtocol:
        """为无 thread metadata 的 session 临时装配 cell，生命周期由调用方关闭。"""
        ...

    async def close_ephemeral_session_cell(
        self,
        cell: ThreadCellProtocol,
        *,
        reason: str = "session_close",
    ) -> None:
        """关闭无 thread metadata 的临时 cell，并释放关联运行资源。"""
        ...

    async def evict_cell(
        self,
        thread_id: str,
        reason: EvictReason,
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

    async def create_and_bind_claude_thread(
        self,
        *,
        claude_thread_id: str,
        cwd: str,
        name: str,
        preset_id: str = "",
    ) -> tuple[ThreadMetadata, bool]: ...

    async def bind_codex_thread(
        self,
        thread_id: str,
        codex_thread_id: str,
        cwd: str,
    ) -> ThreadMetadata: ...

    # usage-token-v2-bigbang：``usage_manager`` 是 :class:`web.usage.usage_token_v2.
    # UsageTokenManager` 实例，唯一公共方法 ``get_thread_usage(thread_id)``。
    # v1 的 record_run_usage / set_last_assistant_usage / get_thread_summary
    # 等方法全部删除。

    @property
    def usage_manager(self) -> Any:
        """:class:`web.usage.usage_token_v2.UsageTokenManager` 实例（v2 无状态门面）。

        ``Any`` 而非具体类型——避免 ``web.threads.types`` Protocol 文件 import
        ``web.usage.usage_token_v2``（保持 types.py 零运行时依赖）。具体类型由
        ``web.threads.manager.ThreadManager.usage_manager`` 提供。
        """
        ...


__all__ = [
    "ThreadCellProtocol",
    "ThreadManagerProtocol",
    "ThreadPermissionsManagerProtocol",
]
