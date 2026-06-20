"""子 agent 生命周期注册表。

功能：为所有来源的子 agent spawn 提供统一 started/completed/failed/cancelled
事件入口和 thread 维度状态查询。
作用：让 workflow、chat 等调用方只依赖 SubAgentLifecycleRegistry，UI 或其他
消费者通过 SubAgentLifecycleStore 读取当前 thread 的活跃和近期子 agent。
关键执行流程：SubAgentManager 发出生命周期事件，registry 先更新 store，再通知
已注册 listener；listener 异常被隔离并写 warning，子 agent 主流程继续执行。
关键函数：record_started 写入运行中记录，record_finished 收口结束状态，
list_thread 返回当前 thread 记录，_notify fan-out 生命周期事件。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Literal

SubAgentLifecycleStatus = Literal["running", "completed", "failed", "cancelled"]
SubAgentLifecycleEventType = Literal["started", "completed", "failed", "cancelled"]

SubAgentLifecycleListener = Callable[["SubAgentLifecycleEvent"], None]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubAgentLifecycleRecord:
    """单个子 agent 运行的当前生命周期状态。

    职责：承载 thread、来源、任务、子 session、状态和时间戳字段。
    关键输入：由 record_started 或 record_finished 根据运行坐标构造。
    关键输出：to_dict 输出 REST 和事件 listener 可消费的字典。
    """

    thread_id: str
    source: str
    workflow_id: str | None
    task_id: str
    task_run_id: str
    task_name: str
    session_id: str
    status: SubAgentLifecycleStatus
    started_at: str
    updated_at: str
    finished_at: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        """转换为字典，输入为当前记录，输出为 JSON 友好的字段映射。"""
        return asdict(self)


@dataclass(frozen=True)
class SubAgentLifecycleEvent:
    """注册表发出的追加式生命周期事件。

    职责：把事件类型和当前记录组合成监听器可处理的事件对象。
    关键输入：event_type 标识 started/completed/failed/cancelled，record 承载状态。
    关键输出：to_dict 输出包含 event_type 的事件字典。
    """

    event_type: SubAgentLifecycleEventType
    record: SubAgentLifecycleRecord

    def to_dict(self) -> dict[str, object]:
        """转换为字典，输入为当前事件，输出为含 event_type 的字段映射。"""
        payload = self.record.to_dict()
        payload["event_type"] = self.event_type
        return payload


class SubAgentLifecycleStore:
    """按 thread 保存子 agent 生命周期状态的内存 store。

    职责：维护每个 thread 当前活跃和近期结束的子 agent 记录。
    关键输入：record_started 和 record_finished 传入运行坐标与状态。
    关键输出：list_thread 返回指定 thread 的记录列表。
    """

    def __init__(self, *, max_records_per_thread: int = 200) -> None:
        """初始化 store，输入为每个 thread 保留数量，输出为可写入的空 store。"""
        self._max_records_per_thread = max_records_per_thread
        self._records_by_thread: dict[str, dict[str, SubAgentLifecycleRecord]] = {}
        self._lock = RLock()

    def record_started(
        self,
        *,
        thread_id: str,
        source: str,
        workflow_id: str | None,
        task_id: str,
        task_run_id: str,
        task_name: str,
        session_id: str,
    ) -> SubAgentLifecycleEvent:
        """记录启动事件，输入为运行坐标，输出为 started 生命周期事件。"""
        now = _now_iso()
        record = SubAgentLifecycleRecord(
            thread_id=thread_id,
            source=_normalize_source(source),
            workflow_id=_clean_optional(workflow_id),
            task_id=task_id,
            task_run_id=task_run_id,
            task_name=task_name,
            session_id=session_id,
            status="running",
            started_at=now,
            updated_at=now,
        )
        with self._lock:
            bucket = self._records_by_thread.setdefault(thread_id, {})
            bucket[_record_key(record)] = record
            self._prune_thread_locked(thread_id)
        return SubAgentLifecycleEvent(event_type="started", record=record)

    def record_finished(
        self,
        *,
        thread_id: str,
        source: str,
        workflow_id: str | None,
        task_id: str,
        task_run_id: str,
        task_name: str,
        session_id: str,
        status: Literal["completed", "failed", "cancelled"],
        error_message: str | None = None,
    ) -> SubAgentLifecycleEvent:
        """记录结束事件，输入为运行坐标、结束状态和错误信息，输出为结束事件。"""
        now = _now_iso()
        source = _normalize_source(source)
        workflow_id = _clean_optional(workflow_id)
        key = _record_key_values(
            source=source,
            workflow_id=workflow_id,
            task_run_id=task_run_id,
            session_id=session_id,
        )
        with self._lock:
            bucket = self._records_by_thread.setdefault(thread_id, {})
            existing = bucket.get(key)
            if existing is None:
                logger.warning(
                    "subagent lifecycle finished before start: thread_id=%s source=%s "
                    "workflow_id=%s task_run_id=%s session_id=%s status=%s",
                    thread_id,
                    source,
                    workflow_id,
                    task_run_id,
                    session_id,
                    status,
                )
            started_at = existing.started_at if existing is not None else now
            record = SubAgentLifecycleRecord(
                thread_id=thread_id,
                source=source,
                workflow_id=workflow_id,
                task_id=task_id,
                task_run_id=task_run_id,
                task_name=task_name,
                session_id=session_id,
                status=status,
                started_at=started_at,
                updated_at=now,
                finished_at=now,
                error_message=_clean_optional(error_message),
            )
            bucket[key] = record
            self._prune_thread_locked(thread_id)
        return SubAgentLifecycleEvent(event_type=status, record=record)

    def list_thread(self, thread_id: str, *, limit: int = 50) -> list[SubAgentLifecycleRecord]:
        """列出 thread 记录，输入为 thread id 和数量上限，输出为按更新时间排序的记录。"""
        with self._lock:
            records = list(self._records_by_thread.get(thread_id, {}).values())
        records.sort(key=lambda item: (item.status == "running", item.updated_at), reverse=True)
        return records[:limit]

    def clear(self) -> None:
        """清空 store，输入为空，输出为清除后的空状态。"""
        with self._lock:
            self._records_by_thread.clear()

    def _prune_thread_locked(self, thread_id: str) -> None:
        """裁剪单个 thread 记录，输入为 thread id，输出为数量受控的内部 bucket。"""
        bucket = self._records_by_thread.get(thread_id)
        if bucket is None or len(bucket) <= self._max_records_per_thread:
            return
        ordered = sorted(
            bucket.values(),
            key=lambda item: (item.status == "running", item.updated_at),
            reverse=True,
        )
        keep = {_record_key(record) for record in ordered[: self._max_records_per_thread]}
        self._records_by_thread[thread_id] = {
            key: record for key, record in bucket.items() if key in keep
        }


class SubAgentLifecycleRegistry:
    """子 agent 生命周期事件 registry。

    职责：对外提供生命周期写入入口，并把事件分发给注册监听器。
    关键输入：record_started/record_finished 的运行坐标、状态和错误信息。
    关键输出：更新 store 后返回事件对象，监听器异常被隔离。
    """

    def __init__(self, store: SubAgentLifecycleStore | None = None) -> None:
        """初始化注册表，输入为可选 store，输出为可注册监听器的注册表。"""
        self._store = store or SubAgentLifecycleStore()
        self._listeners: list[SubAgentLifecycleListener] = []
        self._lock = RLock()

    @property
    def store(self) -> SubAgentLifecycleStore:
        """返回底层 store，输入为空，输出为当前 registry 使用的 store。"""
        return self._store

    def register(self, listener: SubAgentLifecycleListener) -> None:
        """注册监听器，输入为事件回调，输出为更新后的监听器集合。"""
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def unregister(self, listener: SubAgentLifecycleListener) -> None:
        """移除监听器，输入为事件回调，输出为更新后的监听器集合。"""
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def record_started(
        self,
        *,
        thread_id: str,
        source: str,
        workflow_id: str | None,
        task_id: str,
        task_run_id: str,
        task_name: str,
        session_id: str,
    ) -> SubAgentLifecycleEvent:
        """记录启动事件，输入为运行坐标，输出为 started 生命周期事件。"""
        event = self._store.record_started(
            thread_id=thread_id,
            source=source,
            workflow_id=workflow_id,
            task_id=task_id,
            task_run_id=task_run_id,
            task_name=task_name,
            session_id=session_id,
        )
        self._notify(event)
        return event

    def record_finished(
        self,
        *,
        thread_id: str,
        source: str,
        workflow_id: str | None,
        task_id: str,
        task_run_id: str,
        task_name: str,
        session_id: str,
        status: Literal["completed", "failed", "cancelled"],
        error_message: str | None = None,
    ) -> SubAgentLifecycleEvent:
        """记录结束事件，输入为运行坐标、结束状态和错误信息，输出为结束事件。"""
        event = self._store.record_finished(
            thread_id=thread_id,
            source=source,
            workflow_id=workflow_id,
            task_id=task_id,
            task_run_id=task_run_id,
            task_name=task_name,
            session_id=session_id,
            status=status,
            error_message=error_message,
        )
        self._notify(event)
        return event

    def list_thread(self, thread_id: str, *, limit: int = 50) -> list[SubAgentLifecycleRecord]:
        """查询 thread 记录，输入为 thread id 和数量上限，输出为生命周期记录列表。"""
        return self._store.list_thread(thread_id, limit=limit)

    def clear(self) -> None:
        """清空注册表状态，输入为空，输出为底层 store 已清空。"""
        self._store.clear()

    def _notify(self, event: SubAgentLifecycleEvent) -> None:
        """通知监听器，输入为生命周期事件，输出为逐个监听器的处理结果。"""
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception as exc:
                logger.warning(
                    "subagent lifecycle listener failed: %s",
                    type(exc).__name__,
                    exc_info=True,
                )


_DEFAULT_REGISTRY = SubAgentLifecycleRegistry()


def get_default_subagent_lifecycle_registry() -> SubAgentLifecycleRegistry:
    """返回默认注册表，输入为空，输出为进程级生命周期注册表。"""
    return _DEFAULT_REGISTRY


def _record_key(record: SubAgentLifecycleRecord) -> str:
    """构造记录 key，输入为生命周期记录，输出为 store 内部去重键。"""
    return _record_key_values(
        source=record.source,
        workflow_id=record.workflow_id,
        task_run_id=record.task_run_id,
        session_id=record.session_id,
    )


def _record_key_values(
    *,
    source: str,
    workflow_id: str | None,
    task_run_id: str,
    session_id: str,
) -> str:
    """构造记录 key，输入为来源和运行坐标，输出为稳定字符串键。"""
    return "\x1f".join([source, workflow_id or "", task_run_id, session_id])


def _normalize_source(source: str) -> str:
    """归一化事件来源，输入为原始 source，输出为非空来源字符串。"""
    value = source.strip()
    return value or "unknown"


def _clean_optional(value: str | None) -> str | None:
    """清理可选字符串，输入为原始值，输出为去空白后的值或 None。"""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _now_iso() -> str:
    """生成当前 UTC 时间，输入为空，输出为 ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


__all__ = [
    "SubAgentLifecycleEvent",
    "SubAgentLifecycleRecord",
    "SubAgentLifecycleRegistry",
    "SubAgentLifecycleStatus",
    "SubAgentLifecycleStore",
    "get_default_subagent_lifecycle_registry",
]
