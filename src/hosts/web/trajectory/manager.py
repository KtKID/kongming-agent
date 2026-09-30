"""TrajectoryManager：轨迹模块门户（track-timeline-v0.1 · task-trajectory-backend）。

职责（模块对外唯一入口）：

1. 定位 thread 的轨迹双源（``<session_root>/<thread_id>/`` 下）：

   - ``trace.jsonl``：事件日志（时序 / 耗时 / token / 工具入参出参）；
   - ``system_prompt.json``：系统提示词全文快照；
   - session jsonl（``manifest.json`` 的 ``format`` 字段指向）：用户输入
     与 assistant 回复/思考全文。trace 事件里的消息是脱敏摘要
     （``message_roles`` / ``content_chars``），正文只在 session 真源。

2. 读文件（线程池 IO；文件缺失 / 不可读 → 该源为空，不是错误）。
3. 调 :class:`hosts.web.trajectory.projector.TrajectoryProjector` 双源投影
   并回填 ``thread_id``。

thread 是否存在由路由层判断（404 语义），本类不感知 thread 生命周期——
那是 ``hosts.web.threads`` 模块的职责（模块间只调门户）。session 文件的
定位逻辑与 ``hosts.web.run`` 落盘路径、``threads`` 模块 locator 同构
（``manifest.json`` 的 ``format`` 字段是 jsonl 文件名真源）。

v0.1 为历史只读投影，不做缓存与实时推送（实时轨迹留待 v0.2 接
``trajectory.event`` WS 帧，``seq`` = trace 行号 即断点续传坐标）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from hosts.web.protocol.rest_models import TrajectoryResponseDTO
from hosts.web.trajectory.models import (
    SessionMessageRecord,
    SystemPromptSnapshot,
)
from hosts.web.trajectory.projector import TrajectoryProjector
from infrastructure.config.models import Config
from infrastructure.config.paths import resolve_kongming_path

logger = logging.getLogger(__name__)


class TrajectoryManager:
    """轨迹时间线模块门户：thread → 双源文件 → TrajectoryResponseDTO。"""

    def __init__(self, home: Path, cfg: Config) -> None:
        """登记 kongming_home 与配置；不做任何 IO（惰性读文件）。"""
        self._home = home
        self._cfg = cfg
        self._projector = TrajectoryProjector()

    async def get_trajectory(self, thread_id: str) -> TrajectoryResponseDTO:
        """返回 thread 的完整轨迹；双源全缺 → 空轨迹（不是错误）。"""
        session_dir = self.session_dir(thread_id)
        lines, messages, system_prompt = await asyncio.to_thread(self._read_sources, session_dir)
        dto = self._projector.project(
            lines,
            session_messages=messages,
            system_prompt=system_prompt,
        )
        return dto.model_copy(update={"thread_id": thread_id})

    def trace_path(self, thread_id: str) -> Path:
        """定位 thread 的 trace.jsonl 路径（与 run.py 落盘路径同源）。"""
        return self.session_dir(thread_id) / "trace.jsonl"

    def session_dir(self, thread_id: str) -> Path:
        """定位 thread 的 session 目录（<session_root>/<thread_id>）。"""
        session_root = resolve_kongming_path(
            self._cfg.session.file_store_path,
            kongming_home=self._home,
        )
        return session_root / thread_id

    # ------------------------------------------------------------------
    # 内部：双源读取
    # ------------------------------------------------------------------

    @staticmethod
    def _read_sources(
        session_dir: Path,
    ) -> tuple[list[str], list[SessionMessageRecord], SystemPromptSnapshot | None]:
        """一次性读 trace 行 + session 正文消息 + 系统提示词快照。

        任何文件缺失 / 损坏都降级为空源，不抛异常——轨迹是只读观测面，
        单源损坏不应导致整页 500（损坏 trace 行由 projector 落占位记录）。
        """
        lines = TrajectoryManager._read_trace_lines(session_dir / "trace.jsonl")
        messages = TrajectoryManager._read_session_messages(session_dir)
        system_prompt = TrajectoryManager._read_system_prompt(session_dir / "system_prompt.json")
        return lines, messages, system_prompt

    @staticmethod
    def _read_trace_lines(path: Path) -> list[str]:
        """读 trace 文件为行列表；文件缺失 / 不可读返回空列表。"""
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    @staticmethod
    def _read_session_messages(
        session_dir: Path,
    ) -> list[SessionMessageRecord]:
        """读 session jsonl 中带正文的消息（user / assistant）。

        jsonl 文件名以 ``manifest.json`` 的 ``format`` 字段为真源，缺
        manifest / 缺字段时回退 ``<thread_id>.jsonl``。单行损坏跳过。
        """
        messages_path = TrajectoryManager._resolve_session_jsonl(session_dir)
        if messages_path is None:
            return []
        try:
            raw_lines = messages_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[SessionMessageRecord] = []
        for line in raw_lines:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("record_type") != "message":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if role not in ("user", "assistant"):
                continue
            created_at = record.get("created_at")
            created_at_ms = (
                int(float(created_at) * 1000)
                if isinstance(created_at, (int, float)) and not isinstance(created_at, bool)
                else 0
            )
            reasoning = record.get("reasoning_content")
            records.append(
                SessionMessageRecord(
                    role=str(role),
                    content=content if isinstance(content, str) else "",
                    created_at_ms=created_at_ms,
                    reasoning=reasoning if isinstance(reasoning, str) else None,
                )
            )
        return records

    @staticmethod
    def _resolve_session_jsonl(session_dir: Path) -> Path | None:
        """按 manifest.format 真源定位 session jsonl；缺失回退默认名。"""
        candidate = session_dir / f"{session_dir.name}.jsonl"
        try:
            manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return candidate
        fmt = manifest.get("format") if isinstance(manifest, dict) else None
        if isinstance(fmt, str) and fmt:
            return session_dir / fmt
        return candidate

    @staticmethod
    def _read_system_prompt(path: Path) -> SystemPromptSnapshot | None:
        """读系统提示词快照；缺失 / 损坏返回 None。"""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        content = raw.get("content")
        if not isinstance(content, str) or not content:
            return None
        created_at = raw.get("created_at")
        created_at_ms = (
            int(float(created_at) * 1000)
            if isinstance(created_at, (int, float)) and not isinstance(created_at, bool)
            else None
        )
        return SystemPromptSnapshot(content=content, created_at_ms=created_at_ms)
