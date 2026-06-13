"""子 agent FileSession JSONL conversation 懒加载。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hosts.web.workflow_viewer.artifact_reader import _is_relative_to
from hosts.web.workflow_viewer.models import (
    ConversationDTO,
    ConversationMessageDTO,
    WorkflowDiagnosticDTO,
)


class ConversationLoader:
    """读取 subagent.json 指向的 FileSession JSONL。"""

    def __init__(self, *, session_root: Path, workspace_root: Path) -> None:
        self.session_root = session_root.resolve()
        self.workspace_root = workspace_root.resolve()

    def load(
        self,
        *,
        thread_id: str,
        workflow_id: str,
        task_run_id: str,
        subagent_json: dict[str, Any],
        cursor: int = 0,
        limit: int = 100,
    ) -> ConversationDTO:
        diagnostics: list[WorkflowDiagnosticDTO] = []
        source = self._resolve_log_path(subagent_json, diagnostics)
        session_id = _str_or_none(subagent_json.get("session_id"))
        if source is None or not source.is_file():
            diagnostics.append(
                WorkflowDiagnosticDTO(
                    code="conversation.missing",
                    severity="warning",
                    message="子 agent FileSession JSONL 不可读",
                )
            )
            return ConversationDTO(
                thread_id=thread_id,
                workflow_id=workflow_id,
                task_run_id=task_run_id,
                child_session_id=session_id,
                diagnostics=diagnostics,
            )
        messages: list[ConversationMessageDTO] = []
        next_cursor: str | None = None
        start = max(cursor, 0)
        max_count = max(min(limit, 300), 1)
        try:
            with source.open(encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index < start:
                        continue
                    if len(messages) >= max_count:
                        next_cursor = str(index)
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError as exc:
                        diagnostics.append(
                            WorkflowDiagnosticDTO(
                                code="conversation.jsonl_decode_failed",
                                severity="warning",
                                message=f"第 {index + 1} 行解析失败: {exc}",
                            )
                        )
                        continue
                    if isinstance(raw, dict):
                        messages.append(_message_from_record(index, raw))
        except OSError as exc:
            diagnostics.append(
                WorkflowDiagnosticDTO(
                    code="conversation.read_failed",
                    severity="warning",
                    message=f"读取 conversation 失败: {type(exc).__name__}: {exc}",
                )
            )
        return ConversationDTO(
            thread_id=thread_id,
            workflow_id=workflow_id,
            task_run_id=task_run_id,
            child_session_id=session_id,
            source_path=_relative_to_session_root(source, self.session_root),
            messages=messages,
            next_cursor=next_cursor,
            diagnostics=diagnostics,
        )

    def conversation_available(
        self, subagent_json: dict[str, Any]
    ) -> tuple[bool, str | None, list[WorkflowDiagnosticDTO]]:
        diagnostics: list[WorkflowDiagnosticDTO] = []
        source = self._resolve_log_path(subagent_json, diagnostics)
        if source is None or not source.is_file():
            return False, None, diagnostics
        return True, _relative_to_session_root(source, self.session_root), diagnostics

    def _resolve_log_path(
        self, subagent_json: dict[str, Any], diagnostics: list[WorkflowDiagnosticDTO]
    ) -> Path | None:
        raw_path = _str_or_none(subagent_json.get("child_session_log_path"))
        for candidate in self._path_candidates(raw_path):
            if _is_relative_to(candidate, self.session_root) and candidate.is_file():
                return candidate
            if candidate.exists() and not _is_relative_to(candidate, self.session_root):
                diagnostics.append(
                    WorkflowDiagnosticDTO(
                        code="conversation.path_outside_session_root",
                        severity="warning",
                        message="child_session_log_path 位于 session store 根目录外，已拒绝",
                        path=str(candidate),
                    )
                )
        session_id = _str_or_none(subagent_json.get("session_id"))
        if session_id:
            fallback = (self.session_root / session_id / f"{session_id}.jsonl").resolve()
            if _is_relative_to(fallback, self.session_root) and fallback.is_file():
                diagnostics.append(
                    WorkflowDiagnosticDTO(
                        code="conversation.session_id_fallback",
                        severity="info",
                        message="child_session_log_path 不可用，已按 session_id fallback",
                        path=_relative_to_session_root(fallback, self.session_root),
                    )
                )
                return fallback
        return None

    def _path_candidates(self, raw_path: str | None) -> list[Path]:
        if not raw_path:
            return []
        raw = Path(raw_path)
        if raw.is_absolute():
            return [raw.resolve()]
        return [
            (self.workspace_root / raw).resolve(),
            (self.session_root / raw).resolve(),
        ]


def _message_from_record(index: int, raw: dict[str, Any]) -> ConversationMessageDTO:
    message = raw.get("message")
    if not isinstance(message, dict):
        message = {}
    content = message.get("content")
    if not isinstance(content, str):
        content = (
            json.dumps(content, ensure_ascii=False, default=str) if content is not None else ""
        )
    tool_calls = message.get("tool_calls")
    return ConversationMessageDTO(
        record_index=index,
        role=_str_or_none(message.get("role")) or "unknown",
        content=content,
        created_at=raw.get("created_at"),
        message_type=_str_or_none(message.get("name")) or _str_or_none(message.get("type")),
        tool_calls=tool_calls if isinstance(tool_calls, list) else [],
        usage=raw.get("usage") if isinstance(raw.get("usage"), dict) else None,
        raw=raw,
    )


def _relative_to_session_root(path: Path, session_root: Path) -> str:
    try:
        return path.resolve().relative_to(session_root.resolve()).as_posix()
    except ValueError:
        return path.name


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None
