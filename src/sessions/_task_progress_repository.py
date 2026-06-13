"""Session 任务进度内部仓储。

本脚本负责把当前 session 的 task_progress.json 读写到 file session 同目录。
关键流程：从 Config 解析 session 根目录，校验 session_id，读取缺失时返回空快照，写入时原子替换目标文件。
关键函数：from_config 解析路径，progress_path 定位文件，read 读取快照，write 校验并落盘。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from infrastructure.config.models import Config
from infrastructure.config.paths import resolve_kongming_path
from sessions.task_progress_models import (
    TaskProgressSnapshot,
    TaskProgressSource,
    compute_counts,
    current_time_ms,
    snapshot_to_dict,
)


class SessionTaskProgressRepository:
    """读写 `<session_root>/<session_id>/task_progress.json` 的内部 helper。"""

    filename = "task_progress.json"

    def __init__(self, session_root: Path) -> None:
        self._session_root = session_root

    @classmethod
    def from_config(cls, config: Config) -> SessionTaskProgressRepository:
        """从配置解析 file session 根目录。"""
        return cls(resolve_kongming_path(config.session.file_store_path))

    @property
    def session_root(self) -> Path:
        """返回已解析的 session 根路径。"""
        return self._session_root

    def progress_path(self, session_id: str) -> Path:
        """返回当前 session 的任务进度文件路径。"""
        self._validate_session_id(session_id)
        return self._session_root / session_id / self.filename

    def read(self, session_id: str) -> TaskProgressSnapshot:
        """读取快照；文件缺失时返回空快照。"""
        path = self.progress_path(session_id)
        if not path.exists():
            return self._empty_snapshot(session_id)

        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError(f"task progress file must contain a JSON object: {path}")
        raw = self._with_counts(raw)

        snapshot = TaskProgressSnapshot.model_validate(raw)
        if snapshot.session_id != session_id:
            raise ValueError(
                "task progress session_id does not match path session_id: "
                f"session_id={session_id}, path={path}"
            )

        tasks = sorted(snapshot.tasks, key=lambda item: item.display_order)
        return snapshot.model_copy(update={"tasks": tasks, "counts": compute_counts(tasks)})

    def write(
        self,
        session_id: str,
        snapshot: TaskProgressSnapshot,
        *,
        expected_source: TaskProgressSource,
    ) -> TaskProgressSnapshot:
        """校验 source 与 session 后原子写入快照。"""
        self._validate_session_id(session_id)
        if snapshot.session_id != session_id:
            raise ValueError("snapshot session_id does not match target session_id")
        if snapshot.source != expected_source:
            raise ValueError(f"snapshot source must be {expected_source!r}")

        tasks = sorted(snapshot.tasks, key=lambda item: item.display_order)
        updated = snapshot.model_copy(
            update={
                "schema_version": 1,
                "tasks": tasks,
                "counts": compute_counts(tasks),
                "updated_at_ms": snapshot.updated_at_ms or current_time_ms(),
            }
        )

        path = self.progress_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock(path):
            tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(snapshot_to_dict(updated), f, ensure_ascii=False, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
            finally:
                tmp_path.unlink(missing_ok=True)
        return updated

    def _empty_snapshot(self, session_id: str) -> TaskProgressSnapshot:
        now = current_time_ms()
        return TaskProgressSnapshot(
            schema_version=1,
            session_id=session_id,
            updated_at_ms=now,
            source="api",
            tasks=[],
            counts=compute_counts([]),
        )

    def _with_counts(self, raw: dict[str, object]) -> dict[str, object]:
        """为旧快照补齐 counts，输入为原始 JSON 对象，输出为可校验 payload。"""
        if "counts" in raw:
            return raw
        tasks = raw.get("tasks")
        if not isinstance(tasks, list):
            return raw
        counts = {
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "total": len(tasks),
        }
        for item in tasks:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            if status in counts and status != "total":
                counts[status] += 1
        return {**raw, "counts": counts}

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
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        candidate = session_id.strip()
        if candidate in {".", ".."} or "/" in candidate or "\\" in candidate:
            raise ValueError("session_id must be a single path segment")


__all__ = ["SessionTaskProgressRepository"]
