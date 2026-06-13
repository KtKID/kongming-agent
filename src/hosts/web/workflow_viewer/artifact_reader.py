"""Agent workflow artifact 读取器。"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from hosts.web.workflow_viewer.models import (
    WorkflowArtifactContentDTO,
    WorkflowArtifactRefDTO,
    WorkflowDiagnosticDTO,
)

_MAX_TEXT_BYTES = 512_000
_MAX_JSONL_LINES = 2_000
ArtifactKind = Literal["json", "jsonl", "markdown", "text", "directory"]


class WorkflowArtifactReader:
    """读取 workflow 目录内的 JSON、JSONL、Markdown 和 artifact 内容。"""

    def __init__(self, workflow_dir: Path) -> None:
        self.workflow_dir = workflow_dir.resolve()

    def read_json(
        self, relative_path: str
    ) -> tuple[dict[str, Any] | None, list[WorkflowDiagnosticDTO]]:
        path = self._resolve_relative(relative_path)
        if path is None or not path.is_file():
            return None, [self._missing(relative_path)]
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, [self._read_error(relative_path, exc)]
        if not isinstance(payload, dict):
            return None, [
                WorkflowDiagnosticDTO(
                    code="artifact.invalid_json_object",
                    severity="warning",
                    message=f"{relative_path} 不是 JSON object",
                    path=relative_path,
                )
            ]
        return payload, []

    def read_json_any(self, relative_path: str) -> tuple[Any | None, list[WorkflowDiagnosticDTO]]:
        path = self._resolve_relative(relative_path)
        if path is None or not path.is_file():
            return None, [self._missing(relative_path)]
        try:
            return json.loads(path.read_text("utf-8")), []
        except (OSError, json.JSONDecodeError) as exc:
            return None, [self._read_error(relative_path, exc)]

    def read_jsonl(
        self, relative_path: str, *, max_lines: int = _MAX_JSONL_LINES
    ) -> tuple[list[dict[str, Any]], list[WorkflowDiagnosticDTO]]:
        path = self._resolve_relative(relative_path)
        if path is None or not path.is_file():
            return [], [self._missing(relative_path)]
        diagnostics: list[WorkflowDiagnosticDTO] = []
        rows: list[dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index >= max_lines:
                        diagnostics.append(
                            WorkflowDiagnosticDTO(
                                code="artifact.jsonl_truncated",
                                severity="info",
                                message=f"{relative_path} 已按 {max_lines} 行截断",
                                path=relative_path,
                            )
                        )
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as exc:
                        diagnostics.append(self._read_error(f"{relative_path}:{index + 1}", exc))
                        continue
                    if isinstance(item, dict):
                        rows.append(item)
        except OSError as exc:
            diagnostics.append(self._read_error(relative_path, exc))
        return rows, diagnostics

    def read_text(
        self, relative_path: str, *, max_bytes: int = _MAX_TEXT_BYTES
    ) -> tuple[str | None, bool, list[WorkflowDiagnosticDTO]]:
        path = self._resolve_relative(relative_path)
        if path is None or not path.is_file():
            return None, False, [self._missing(relative_path)]
        try:
            data = path.read_bytes()
        except OSError as exc:
            return None, False, [self._read_error(relative_path, exc)]
        truncated = len(data) > max_bytes
        text = data[:max_bytes].decode("utf-8", errors="replace")
        return text, truncated, []

    def list_artifacts(self) -> list[WorkflowArtifactRefDTO]:
        refs: list[WorkflowArtifactRefDTO] = []
        candidates = [
            "workflow.json",
            "audit.jsonl",
            "result.json",
            "reports/index.json",
            "review_board/context.md",
            "review_board/sources.md",
            "review_board/claims.jsonl",
            "review_board/rebuttals.jsonl",
            "review_board/consensus.md",
            "review_board/final_report.md",
            "map_reduce/shards.json",
            "map_reduce/mappers/index.json",
            "map_reduce/reducer/result.json",
        ]
        seen: set[str] = set()
        for rel in candidates:
            refs.append(self.artifact_ref(rel))
            seen.add(rel)
        for rel in self._iter_extra_artifacts():
            if rel not in seen:
                refs.append(self.artifact_ref(rel))
                seen.add(rel)
        return refs

    def artifact_ref(self, relative_path: str) -> WorkflowArtifactRefDTO:
        path = self._resolve_relative(relative_path)
        available = path is not None and path.exists()
        kind = _kind_for_path(relative_path, path)
        size = None
        if available and path is not None and path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                size = None
        return WorkflowArtifactRefDTO(
            artifact_id=encode_artifact_id(relative_path),
            path=relative_path,
            kind=kind,
            title=_title_for_path(relative_path),
            size_bytes=size,
            available=available,
            missing_reason=None if available else "missing",
        )

    def read_artifact_content(self, artifact_id: str) -> WorkflowArtifactContentDTO:
        relative_path = decode_artifact_id(artifact_id)
        ref = self.artifact_ref(relative_path)
        if not ref.available:
            return WorkflowArtifactContentDTO(
                artifact_id=artifact_id,
                path=relative_path,
                kind=ref.kind,
                title=ref.title,
                diagnostics=[self._missing(relative_path)],
            )
        if ref.kind == "directory":
            entries = [
                item.name
                for item in sorted(
                    (self.workflow_dir / relative_path).iterdir(), key=lambda p: p.name
                )
            ]
            return WorkflowArtifactContentDTO(
                artifact_id=artifact_id,
                path=relative_path,
                kind=ref.kind,
                title=ref.title,
                content=entries,
            )
        if ref.kind == "json":
            payload, diagnostics = self.read_json_any(relative_path)
            return WorkflowArtifactContentDTO(
                artifact_id=artifact_id,
                path=relative_path,
                kind=ref.kind,
                title=ref.title,
                content=payload,
                diagnostics=diagnostics,
            )
        if ref.kind == "jsonl":
            payload, diagnostics = self.read_jsonl(relative_path)
            return WorkflowArtifactContentDTO(
                artifact_id=artifact_id,
                path=relative_path,
                kind=ref.kind,
                title=ref.title,
                content=payload,
                diagnostics=diagnostics,
            )
        text, truncated, diagnostics = self.read_text(relative_path)
        return WorkflowArtifactContentDTO(
            artifact_id=artifact_id,
            path=relative_path,
            kind=ref.kind,
            title=ref.title,
            content=text,
            truncated=truncated,
            diagnostics=diagnostics,
        )

    def _iter_extra_artifacts(self) -> Iterable[str]:
        for directory in ("reports", "agents"):
            root = self.workflow_dir / directory
            if not root.exists():
                continue
            for path in sorted(root.rglob("*"), key=lambda p: str(p)):
                if path.is_file() and path.suffix.lower() in {".json", ".jsonl", ".md", ".txt"}:
                    yield _relative_to(path, self.workflow_dir)

    def _resolve_relative(self, relative_path: str) -> Path | None:
        candidate = (self.workflow_dir / relative_path).resolve()
        if not _is_relative_to(candidate, self.workflow_dir):
            return None
        return candidate

    @staticmethod
    def _missing(relative_path: str) -> WorkflowDiagnosticDTO:
        return WorkflowDiagnosticDTO(
            code="artifact.missing",
            severity="warning",
            message=f"缺少 artifact: {relative_path}",
            path=relative_path,
        )

    @staticmethod
    def _read_error(relative_path: str, exc: Exception) -> WorkflowDiagnosticDTO:
        return WorkflowDiagnosticDTO(
            code="artifact.read_failed",
            severity="warning",
            message=f"读取 artifact 失败: {type(exc).__name__}: {exc}",
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
    if value.startswith("/") or ".." in Path(value).parts:
        raise ValueError("invalid artifact path")
    return value.replace("\\", "/")


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


def _title_for_path(relative_path: str) -> str:
    return Path(relative_path).name or relative_path


def _relative_to(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
