"""Session backends and discovery helpers."""

from __future__ import annotations

from sessions.file_session import FileSession, ValidationResult
from sessions.session_bootstrap import SessionBootstrap
from sessions.session_discovery import (
    SessionSummary,
    discover_file_sessions,
    discover_sqlite_sessions,
    find_session_by_id,
    most_recent_session,
)
from sessions.session_store import SQLiteSession, build_session

__all__ = [
    "FileSession",
    "SQLiteSession",
    "SessionBootstrap",
    "SessionSummary",
    "ValidationResult",
    "build_session",
    "discover_file_sessions",
    "discover_sqlite_sessions",
    "find_session_by_id",
    "most_recent_session",
]
