"""ThreadArtifactManager — thread 根目录文件只读投影入口。"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from hosts.web.thread_artifacts.models import (
    ThreadArtifactContentDTO,
    ThreadArtifactDiagnosticDTO,
    ThreadArtifactListDTO,
    ThreadArtifactRefDTO,
)
from infrastructure.config.paths import resolve_kongming_path

_MAX_TEXT_BYTES = 512_000
_MAX_JSONL_LINES = 2_000
_ALLOWED_SUFFIXES = {".json", ".jsonl", ".md", ".txt"}
ArtifactKind = Literal["json", "jsonl", "markdown", "text", "directory"]


class ThreadArtifactManager:
    """读取 file session thread 目录，输出 Web 可消费的文件列表和内容 DTO。"""

    def __init__(self, *, config: Any) -> None:
        self.session_root = Path(resolve_kongming_path(config.session.file_store_path)).resolve()

    def list_artifacts(self, thread_id: str) -> ThreadArtifactListDTO:
        """列出 thread 根目录可读文件，输入 thread_id，输出文件 DTO 列表。"""
        thread_dir = self._thread_dir(thread_id)
        diagnostics: list[ThreadArtifactDiagnosticDTO] = []
        if not thread_dir.exists():
            return ThreadArtifactListDTO(
                thread_id=thread_id,
                diagnostics=[
                    ThreadArtifactDiagnosticDTO(
                        code="thread_artifact.thread_dir_missing",
                        severity="warning",
                        message=f"thread 目录不存在: {thread_id}",
                    )
                ],
            )

        refs: list[ThreadArtifactRefDTO] = []
        seen: set[str] = set()
        for rel in self._preferred_paths(thread_id):
            refs.append(self.artifact_ref(thread_id, rel))
            seen.add(rel)
        for rel in self._iter_extra_paths(thread_dir):
            if rel in seen:
                continue
            refs.append(self.artifact_ref(thread_id, rel))
            seen.add(rel)
        return ThreadArtifactListDTO(thread_id=thread_id, files=refs, diagnostics=diagnostics)

    def read_artifact(self, *, thread_id: str, artifact_id: str) -> ThreadArtifactContentDTO:
        """读取 thread artifact 内容，输入 thread_id 与 artifact_id，输出内容 DTO。"""
        relative_path = decode_artifact_id(artifact_id)
        ref = self.artifact_ref(thread_id, relative_path)
        if not ref.available:
            return ThreadArtifactContentDTO(
                artifact_id=artifact_id,
                path=relative_path,
                kind=ref.kind,
                title=ref.title,
                diagnostics=[self._missing(relative_path)],
            )

        path = self._resolve_relative(thread_id, relative_path)
        if path is None:
            raise ValueError("invalid artifact path")
        if ref.kind == "directory":
            return ThreadArtifactContentDTO(
                artifact_id=artifact_id,
                path=relative_path,
                kind=ref.kind,
                title=ref.title,
                content=[
                    item.name
                    for item in sorted(path.iterdir(), key=lambda p: p.name)
                    if not item.name.startswith(".")
                ],
            )
        if ref.kind == "json":
            payload, diagnostics = self._read_json_any(path, relative_path)
            return ThreadArtifactContentDTO(
                artifact_id=artifact_id,
                path=relative_path,
                kind=ref.kind,
                title=ref.title,
                content=payload,
                diagnostics=diagnostics,
            )
        if ref.kind == "jsonl":
            payload, diagnostics = self._read_jsonl(path, relative_path)
            return ThreadArtifactContentDTO(
                artifact_id=artifact_id,
                path=relative_path,
                kind=ref.kind,
                title=ref.title,
                content=payload,
                diagnostics=diagnostics,
            )

        text, truncated, diagnostics = self._read_text(path, relative_path)
        return ThreadArtifactContentDTO(
            artifact_id=artifact_id,
            path=relative_path,
            kind=ref.kind,
            title=ref.title,
            content=text,
            truncated=truncated,
            diagnostics=diagnostics,
        )

    def artifact_ref(self, thread_id: str, relative_path: str) -> ThreadArtifactRefDTO:
        """生成单个 artifact 引用，输入相对路径，输出文件元数据。"""
        path = self._resolve_relative(thread_id, relative_path)
        available = path is not None and path.exists()
        kind = _kind_for_path(relative_path, path)
        size = None
        record_count = None
        if available and path is not None and path.is_file():
            try:
                size = path.stat().st_size
                if kind == "jsonl":
                    record_count = _count_nonempty_lines(path)
            except OSError:
                size = None
        return ThreadArtifactRefDTO(
            artifact_id=encode_artifact_id(relative_path),
            path=relative_path,
            kind=kind,
            title=Path(relative_path).name or relative_path,
            size_bytes=size,
            available=available,
            record_count=record_count,
            missing_reason=None if available else "missing",
        )

    def _thread_dir(self, thread_id: str) -> Path:
        return (self.session_root / thread_id).resolve()

    def _preferred_paths(self, thread_id: str) -> list[str]:
        paths = [
            "manifest.json",
            "system_prompt.json",
            f"{thread_id}.jsonl",
            "trace.jsonl",
            "task_progress.json",
        ]
        workflow_dir = self._thread_dir(thread_id) / "agent-workflows"
        if workflow_dir.exists():
            paths.append("agent-workflows")
        return paths

    def _iter_extra_paths(self, thread_dir: Path) -> Iterable[str]:
        for path in sorted(thread_dir.iterdir(), key=lambda p: p.name):
            if path.name.startswith("."):
                continue
            if path.is_dir():
                if path.name == "agent-workflows":
                    yield path.name
                continue
            if path.suffix.lower() in _ALLOWED_SUFFIXES:
                yield path.relative_to(thread_dir).as_posix()

    def _resolve_relative(self, thread_id: str, relative_path: str) -> Path | None:
        if Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
            return None
        suffix = Path(relative_path).suffix.lower()
        if suffix and suffix not in _ALLOWED_SUFFIXES:
            return None
        if not suffix and relative_path != "agent-workflows":
            return None
        thread_dir = self._thread_dir(thread_id)
        candidate = (thread_dir / relative_path).resolve()
        if not _is_relative_to(candidate, thread_dir):
            return None
        return candidate

    def _read_json_any(
        self, path: Path, relative_path: str
    ) -> tuple[Any | None, list[ThreadArtifactDiagnosticDTO]]:
        try:
            payload = json.loads(path.read_text("utf-8"))
            if relative_path == "system_prompt.json" and isinstance(payload, dict):
                return _redact_system_prompt_payload(payload), []
            return payload, []
        except (OSError, json.JSONDecodeError) as exc:
            return None, [self._read_error(relative_path, exc)]

    def _read_jsonl(
        self, path: Path, relative_path: str
    ) -> tuple[list[dict[str, Any]], list[ThreadArtifactDiagnosticDTO]]:
        rows: list[dict[str, Any]] = []
        diagnostics: list[ThreadArtifactDiagnosticDTO] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index >= _MAX_JSONL_LINES:
                        diagnostics.append(
                            ThreadArtifactDiagnosticDTO(
                                code="thread_artifact.jsonl_truncated",
                                severity="info",
                                message=f"{relative_path} 已按 {_MAX_JSONL_LINES} 行截断",
                                path=relative_path,
                            )
                        )
                        break
                    raw = line.rstrip("\n")
                    if not raw.strip():
                        continue
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        diagnostics.append(self._read_error(f"{relative_path}:{index + 1}", exc))
                        rows.append(
                            {
                                "__parse_error__": True,
                                "line": index + 1,
                                "raw": raw[:500],
                                "error": str(exc),
                            }
                        )
                        continue
                    if isinstance(item, dict):
                        rows.append(item)
                    else:
                        rows.append({"value": item})
        except OSError as exc:
            diagnostics.append(self._read_error(relative_path, exc))
        return rows, diagnostics

    def _read_text(
        self, path: Path, relative_path: str
    ) -> tuple[str | None, bool, list[ThreadArtifactDiagnosticDTO]]:
        try:
            data = path.read_bytes()
        except OSError as exc:
            return None, False, [self._read_error(relative_path, exc)]
        truncated = len(data) > _MAX_TEXT_BYTES
        return data[:_MAX_TEXT_BYTES].decode("utf-8", errors="replace"), truncated, []

    @staticmethod
    def _missing(relative_path: str) -> ThreadArtifactDiagnosticDTO:
        return ThreadArtifactDiagnosticDTO(
            code="thread_artifact.missing",
            severity="warning",
            message=f"缺少 thread artifact: {relative_path}",
            path=relative_path,
        )

    @staticmethod
    def _read_error(relative_path: str, exc: Exception) -> ThreadArtifactDiagnosticDTO:
        return ThreadArtifactDiagnosticDTO(
            code="thread_artifact.read_failed",
            severity="warning",
            message=f"读取 thread artifact 失败: {type(exc).__name__}: {exc}",
            path=relative_path,
        )


def encode_artifact_id(relative_path: str) -> str:
    raw = relative_path.replace("\\", "/").encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_artifact_id(artifact_id: str) -> str:
    padded = artifact_id + "=" * (-len(artifact_id) % 4)
    try:
        value = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid artifact_id") from exc
    value = value.replace("\\", "/")
    if value.startswith("/") or ".." in Path(value).parts:
        raise ValueError("invalid artifact path")
    return value


def _kind_for_path(relative_path: str, path: Path | None) -> ArtifactKind:
    if path is not None and path.is_dir():
        return "directory"
    suffix = Path(relative_path).suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix == ".jsonl":
        return "jsonl"
    if suffix == ".md":
        return "markdown"
    return "text"


def _count_nonempty_lines(path: Path) -> int:
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def _redact_system_prompt_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """移除 system prompt 正文，保留审计定位所需元数据。"""
    redacted = dict(payload)
    content = redacted.pop("content", None)
    if content is not None:
        redacted["content_redacted"] = True
        if isinstance(content, str):
            redacted["content_chars"] = len(content)
    return redacted


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
