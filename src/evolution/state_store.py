"""Self evolution 控制层状态。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from core.clock import now_epoch_ms
from evolution.models import SessionLearningState


def _now_ms() -> int:
    return now_epoch_ms()


class EvolutionStateStore:
    """管理 ``evolution.state.json``。"""

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir.resolve()
        self._path = self._root_dir / "evolution.state.json"
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def record_parent_run(
        self,
        *,
        session_id: str,
        user_turn_count: int,
    ) -> SessionLearningState:
        async with self._lock:
            data = await asyncio.to_thread(self._read_raw)
            sessions = data.setdefault("sessions", {})
            current = SessionLearningState.from_dict(sessions.get(session_id, {}))
            current.run_count += 1
            current.user_turn_count = user_turn_count
            sessions[session_id] = current.to_dict()
            await asyncio.to_thread(self._write_raw, data)
            return current

    async def update_session_meta(
        self,
        *,
        session_id: str,
        user_turn_count: int,
    ) -> SessionLearningState:
        """runtime channel 用：只刷新 user_turn_count 等旁路字段，不递增 run_count。

        run_count 真源已迁移到 session manifest（``Session.get_run_count``）；
        runtime channel（web/CLI Runner）调用本方法记录旁路元数据，**不得**用
        :meth:`record_parent_run` 自行递增——否则会与 manifest 真值产生双源误差。

        claude_code 频道无 session 对象，仍用 :meth:`record_parent_run` 自维护。
        """
        async with self._lock:
            data = await asyncio.to_thread(self._read_raw)
            sessions = data.setdefault("sessions", {})
            current = SessionLearningState.from_dict(sessions.get(session_id, {}))
            current.user_turn_count = user_turn_count
            sessions[session_id] = current.to_dict()
            await asyncio.to_thread(self._write_raw, data)
            return current

    async def mark_review_result(
        self,
        *,
        session_id: str,
        run_id: str,
        status: str,
        reviewed_at_ms: int | None = None,
        nutrient_ids: tuple[str, ...] = (),
    ) -> SessionLearningState:
        async with self._lock:
            data = await asyncio.to_thread(self._read_raw)
            sessions = data.setdefault("sessions", {})
            current = SessionLearningState.from_dict(sessions.get(session_id, {}))
            current.last_reviewed_run_id = run_id
            current.last_review_at_ms = reviewed_at_ms if reviewed_at_ms is not None else _now_ms()
            current.last_review_status = status
            if nutrient_ids:
                current.last_nutrient_id = nutrient_ids[-1]
                current.last_nutrient_at_ms = current.last_review_at_ms
            sessions[session_id] = current.to_dict()
            await asyncio.to_thread(self._write_raw, data)
            return current

    def _read_raw(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": 1, "sessions": {}}
        raw = self._path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("evolution.state.json root must be an object")
        data.setdefault("version", 1)
        data.setdefault("sessions", {})
        return data

    def _write_raw(self, data: dict[str, Any]) -> None:
        self._root_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)
