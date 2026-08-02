"""Session 任务进度内部仓储。

本模块把每个 session 的当前 foreground 快照读写到 task_progress.json。
关键流程：在同一文件锁内读取 v2 快照、执行 Manager 提供的迁移、重算 counts 后原子替换文件。
关键函数：read 读取快照，update 串行化状态迁移，write 写入已构造快照。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from infrastructure.config.models import Config
from infrastructure.config.paths import resolve_kongming_path
from sessions.task_progress_models import (
    TaskProgressSnapshot,
    compute_counts,
    current_time_ms,
    snapshot_to_dict,
)


class SessionTaskProgressRepository:
    """读写 `<session_root>/<session_id>/task_progress.json` 的内部 helper。"""

    filename = "task_progress.json"

    def __init__(self, session_root: Path) -> None:
        """绑定 session 根目录，输入为已解析路径，输出为空。"""
        self._session_root = session_root

    @classmethod
    def from_config(cls, config: Config) -> SessionTaskProgressRepository:
        """从配置解析 session 根目录，输入为 Config，输出为仓储实例。"""
        return cls(resolve_kongming_path(config.session.file_store_path))

    @property
    def session_root(self) -> Path:
        """返回已解析的 session 根路径。"""
        return self._session_root

    def progress_path(self, session_id: str) -> Path:
        """定位当前 session 进度文件，输入为 session ID，输出为文件路径。"""
        self._validate_session_id(session_id)
        return self._session_root / session_id / self.filename

    def read(self, session_id: str) -> TaskProgressSnapshot:
        """读取当前快照，输入为 session ID，输出为 v2 快照或空快照。"""
        path = self.progress_path(session_id)
        return self._read_path_unlocked(session_id, path)

    def update(
        self,
        session_id: str,
        updater: Callable[[TaskProgressSnapshot], TaskProgressSnapshot],
    ) -> TaskProgressSnapshot:
        """串行执行快照迁移，输入为 session 与迁移函数，输出为已落盘快照。"""
        self._validate_session_id(session_id)
        path = self.progress_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock(path):
            current = self._read_path_unlocked(session_id, path)
            return self._write_unlocked(session_id, updater(current), path=path)

    def write(self, session_id: str, snapshot: TaskProgressSnapshot) -> TaskProgressSnapshot:
        """写入完整快照，输入为 session 与快照，输出为规范化落盘快照。"""
        self._validate_session_id(session_id)
        path = self.progress_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock(path):
            return self._write_unlocked(session_id, snapshot, path=path)

    def _read_path_unlocked(self, session_id: str, path: Path) -> TaskProgressSnapshot:
        """读取目标路径，输入为 session 和文件路径，输出为排序且重算计数的 v2 快照。"""
        if not path.exists():
            return self._empty_snapshot(session_id)
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError(f"task progress file must contain a JSON object: {path}")
        snapshot = TaskProgressSnapshot.model_validate(raw)
        if snapshot.session_id != session_id:
            raise ValueError(
                "task progress session_id does not match path session_id: "
                f"session_id={session_id}, path={path}"
            )
        return self._normalize(snapshot)

    def _write_unlocked(
        self,
        session_id: str,
        snapshot: TaskProgressSnapshot,
        *,
        path: Path,
    ) -> TaskProgressSnapshot:
        """在已持锁范围写入快照，输入为 session、快照和路径，输出为原子替换结果。"""
        if snapshot.session_id != session_id:
            raise ValueError("snapshot session_id does not match target session_id")
        updated = self._normalize(snapshot)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(snapshot_to_dict(updated), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return updated

    def _normalize(self, snapshot: TaskProgressSnapshot) -> TaskProgressSnapshot:
        """规范化快照，输入为任意合法 v2 快照，输出为排序计数一致的快照。"""
        tasks = sorted(snapshot.tasks, key=lambda item: item.display_order)
        return snapshot.model_copy(
            update={
                "schema_version": 2,
                "tasks": tasks,
                "counts": compute_counts(tasks),
                "updated_at_ms": snapshot.updated_at_ms or current_time_ms(),
            }
        )

    def _empty_snapshot(self, session_id: str) -> TaskProgressSnapshot:
        """构造空快照，输入为 session ID，输出为无 foreground 坐标的 v2 快照。"""
        return TaskProgressSnapshot(
            schema_version=2,
            session_id=session_id,
            updated_at_ms=current_time_ms(),
            tasks=[],
            counts=compute_counts([]),
        )

    @contextmanager
    def _file_lock(self, path: Path) -> Iterator[None]:
        """串行化同一进度文件写入，输入为目标路径，输出为持锁上下文。"""
        lock_dir = path.with_name(f".{path.name}.lock")
        deadline = time.monotonic() + 5.0
        while True:
            try:
                lock_dir.mkdir()
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out acquiring task progress lock: {lock_dir}"
                    ) from None
                time.sleep(0.01)
        try:
            yield
        finally:
            lock_dir.rmdir()

    def _validate_session_id(self, session_id: str) -> None:
        """校验 session 路径段，输入为 session ID，非法时抛 ValueError。"""
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        candidate = session_id.strip()
        if candidate in {".", ".."} or "/" in candidate or "\\" in candidate:
            raise ValueError("session_id must be a single path segment")


__all__ = ["SessionTaskProgressRepository"]
