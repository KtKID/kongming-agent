"""Thread artifact viewer DTOs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ThreadArtifactDiagnosticDTO(BaseModel):
    code: str
    severity: Literal["info", "warning", "error"] = "warning"
    message: str
    path: str | None = None


class ThreadArtifactRefDTO(BaseModel):
    artifact_id: str
    path: str
    kind: Literal["json", "jsonl", "markdown", "text", "directory"]
    title: str
    size_bytes: int | None = None
    available: bool = True
    record_count: int | None = None
    missing_reason: str | None = None


class ThreadArtifactContentDTO(BaseModel):
    artifact_id: str
    path: str
    kind: str
    title: str
    content: Any = None
    truncated: bool = False
    diagnostics: list[ThreadArtifactDiagnosticDTO] = Field(default_factory=list)


class ThreadArtifactListDTO(BaseModel):
    thread_id: str
    files: list[ThreadArtifactRefDTO] = Field(default_factory=list)
    diagnostics: list[ThreadArtifactDiagnosticDTO] = Field(default_factory=list)
