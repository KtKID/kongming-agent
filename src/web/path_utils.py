"""Cross-platform workspace path helpers for web entrypoints."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath


def is_absolute_workspace_path(value: str) -> bool:
    """Return True for POSIX, drive-letter, and UNC absolute paths."""
    raw = value.strip()
    if not raw:
        return False
    return PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute()


__all__ = ["is_absolute_workspace_path"]
