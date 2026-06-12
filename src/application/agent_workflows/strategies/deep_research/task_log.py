"""Deep Research 子 agent task 落地审计日志。

本脚本负责为 Deep Research 的 planner/searcher/extractor/grouper/juror/reporter task 写入 task.log.jsonl。
作用是把每个子 agent task 的输入、权限、预算、状态、错误和输出 artifact 持久化，并在 subagent.json 与 workflow audit 中建立索引。
关键执行流程：start_task 写 started 事件和索引，complete_task 写 completed 事件，fail_task 写 failed 或 degraded 事件。
关键函数：DeepResearchTaskLogWriter.start_task 开始日志，complete_task 完成日志，fail_task 失败日志。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

TaskStatus = Literal["completed", "degraded", "failed", "cancelled"]


class DeepResearchTaskLogWriter:
    """写入 Deep Research 子 agent task log 和 workflow audit 索引。"""

    def __init__(self, *, workflow_dir: Path, audit_writer: Any | None = None) -> None:
        """初始化 writer，输入为 workflow 目录和可选 audit writer，输出为可写 task log 的实例。"""
        self._workflow_dir = workflow_dir
        self._audit_writer = audit_writer

    def start_task(
        self,
        *,
        task_run_id: str,
        phase: str,
        role: str,
        input_artifacts: Sequence[str] = (),
        prompt_hash: str | None = None,
        tool_allowlist: Sequence[str] = (),
        budget_snapshot: Mapping[str, object] | None = None,
        child_session_log_path: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Path:
        """记录 task 开始，输入为 task 上下文，输出为 task.log.jsonl 路径。"""
        task_log_path = self._task_log_path(task_run_id)
        payload = {
            "event": "started",
            "workflow_id": self._workflow_dir.name,
            "task_run_id": task_run_id,
            "phase": phase,
            "role": role,
            "input_artifacts": list(input_artifacts),
            "prompt_hash": prompt_hash,
            "tool_allowlist": list(tool_allowlist),
            "budget_snapshot": dict(budget_snapshot or {}),
            "child_session_log_path": child_session_log_path,
            "metadata": dict(metadata or {}),
            "ts": _now_iso(),
        }
        self._append(task_log_path, payload)
        self._update_subagent_json(
            task_run_id=task_run_id,
            task_log_path=task_log_path,
            child_session_log_path=child_session_log_path,
            phase=phase,
            role=role,
        )
        self._write_audit(
            "deep_research.subagent_task_started",
            {
                "task_run_id": task_run_id,
                "phase": phase,
                "role": role,
                "task_log_path": self._relative(task_log_path),
                "child_session_log_path": child_session_log_path,
                "budget_snapshot": dict(budget_snapshot or {}),
            },
        )
        return task_log_path

    def complete_task(
        self,
        *,
        task_run_id: str,
        phase: str,
        role: str,
        output_artifacts: Sequence[str] = (),
        status: TaskStatus = "completed",
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        """记录 task 完成，输入为输出 artifact 和状态，输出为日志追加与 audit 事件。"""
        task_log_path = self._task_log_path(task_run_id)
        payload = {
            "event": "completed",
            "workflow_id": self._workflow_dir.name,
            "task_run_id": task_run_id,
            "phase": phase,
            "role": role,
            "status": status,
            "output_artifacts": list(output_artifacts),
            "metadata": dict(metadata or {}),
            "ts": _now_iso(),
        }
        self._append(task_log_path, payload)
        self._update_subagent_json(
            task_run_id=task_run_id,
            task_log_path=task_log_path,
            phase=phase,
            role=role,
            completed_status=status,
        )
        self._write_audit(
            "deep_research.subagent_task_completed",
            {
                "task_run_id": task_run_id,
                "phase": phase,
                "role": role,
                "status": status,
                "output_artifacts": list(output_artifacts),
                "task_log_path": self._relative(task_log_path),
            },
        )

    def fail_task(
        self,
        *,
        task_run_id: str,
        phase: str,
        role: str,
        error_digest: str,
        status: TaskStatus = "failed",
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        """记录 task 失败，输入为错误摘要和状态，输出为日志追加与 audit 事件。"""
        task_log_path = self._task_log_path(task_run_id)
        payload = {
            "event": "failed",
            "workflow_id": self._workflow_dir.name,
            "task_run_id": task_run_id,
            "phase": phase,
            "role": role,
            "status": status,
            "error_digest": error_digest,
            "metadata": dict(metadata or {}),
            "ts": _now_iso(),
        }
        self._append(task_log_path, payload)
        self._update_subagent_json(
            task_run_id=task_run_id,
            task_log_path=task_log_path,
            phase=phase,
            role=role,
            completed_status=status,
        )
        self._write_audit(
            "deep_research.subagent_task_failed",
            {
                "task_run_id": task_run_id,
                "phase": phase,
                "role": role,
                "status": status,
                "error_digest": error_digest,
                "task_log_path": self._relative(task_log_path),
            },
        )

    def _task_log_path(self, task_run_id: str) -> Path:
        """生成 task log 路径，输入为 task_run_id，输出为 workflow 内 JSONL 路径。"""
        return self._workflow_dir / "agents" / task_run_id / "task.log.jsonl"

    def _append(self, path: Path, payload: Mapping[str, object]) -> None:
        """追加 JSONL 事件，输入为路径和 payload，输出为写入一行 JSON。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _update_subagent_json(
        self,
        *,
        task_run_id: str,
        task_log_path: Path,
        phase: str,
        role: str,
        child_session_log_path: str | None = None,
        completed_status: str | None = None,
    ) -> None:
        """更新 subagent.json 索引，输入为 task 信息，输出为 task_log_path 持久化。"""
        path = self._workflow_dir / "agents" / task_run_id / "subagent.json"
        payload = _read_json_object(path)
        payload.setdefault("task_run_id", task_run_id)
        payload["task_log_path"] = str(task_log_path)
        payload["deep_research_phase"] = phase
        payload["deep_research_role"] = role
        if child_session_log_path:
            payload["child_session_log_path"] = child_session_log_path
        if completed_status:
            payload["deep_research_task_status"] = completed_status
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def _write_audit(self, action: str, payload: Mapping[str, object]) -> None:
        """写入 workflow audit，输入为 action 和 payload，输出为 audit writer 追加记录。"""
        if self._audit_writer is None:
            return
        self._audit_writer.write_event({"action": action, "payload": dict(payload)})

    def _relative(self, path: Path) -> str:
        """生成相对 workflow 路径，输入为绝对或相对路径，输出为展示用路径。"""
        try:
            return str(path.relative_to(self._workflow_dir))
        except ValueError:
            return str(path)


def _read_json_object(path: Path) -> dict[str, Any]:
    """读取 JSON 对象，输入为路径，输出为 dict；缺失或损坏时返回空对象。"""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _now_iso() -> str:
    """生成当前 UTC 时间，输入为空，输出为 ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


__all__ = ["DeepResearchTaskLogWriter", "TaskStatus"]
