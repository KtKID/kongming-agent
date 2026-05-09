"""SiTian domain models.

Shared config and runtime-state structures for the SiTian scanning loop.
They live in ``core`` so future scanners, stores, and APIs can reuse one schema.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SiTianSourceConfig(BaseModel):
    """Single SiTian source declaration."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["generic_channel", "claude_project", "codex_project"]
    path: str
    scan_interval_sec: int | None = Field(default=None, gt=0)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("id", "path")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("source id/path must not be empty")
        return stripped

    def resolved_scan_interval_sec(self, default_scan_interval_sec: int) -> int:
        """Return the effective scan interval for this source."""
        return self.scan_interval_sec or default_scan_interval_sec


class SiTianConfig(BaseModel):
    """Top-level SiTian config section."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["v1"] = "v1"
    default_scan_interval_sec: int = Field(default=300, gt=0)
    idle_sleep_sec: int = Field(default=30, gt=0)
    sources: list[SiTianSourceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_source_ids(self) -> SiTianConfig:
        seen: set[str] = set()
        for source in self.sources:
            if source.id in seen:
                raise ValueError(
                    f"sitian.sources contains duplicate id={source.id!r}; source id must be unique"
                )
            seen.add(source.id)
        return self


class SiTianSourceRuntimeState(BaseModel):
    """Runtime state for one SiTian source."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    scan_interval_sec: int = Field(gt=0)
    retry_backoff_sec: int = Field(default=0, ge=0)
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    next_run_at: datetime
    status: Literal["idle", "running", "error"] = "idle"

    @field_validator("source_id")
    @classmethod
    def _source_id_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("source_id must not be empty")
        return stripped

    @classmethod
    def from_source_config(
        cls,
        source: SiTianSourceConfig,
        *,
        default_scan_interval_sec: int,
        now: datetime | None = None,
        retry_backoff_sec: int = 0,
        status: Literal["idle", "running", "error"] = "idle",
    ) -> SiTianSourceRuntimeState:
        """Build initial runtime state from config."""
        effective_now = now or datetime.now(timezone.utc)
        return cls(
            source_id=source.id,
            scan_interval_sec=source.resolved_scan_interval_sec(default_scan_interval_sec),
            retry_backoff_sec=retry_backoff_sec,
            next_run_at=effective_now,
            status=status,
        )


__all__ = [
    "SiTianConfig",
    "SiTianSourceConfig",
    "SiTianSourceRuntimeState",
]
