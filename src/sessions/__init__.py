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
from sessions.task_progress_manager import SessionTaskProgressManager
from sessions.task_progress_models import (
    TASK_PROGRESS_MAX_DESC_LENGTH,
    TASK_PROGRESS_MAX_ERROR_LENGTH,
    TASK_PROGRESS_MAX_ID_LENGTH,
    TASK_PROGRESS_MAX_ITEMS,
    TaskProgressCounts,
    TaskProgressItem,
    TaskProgressSnapshot,
    TaskProgressSource,
    TaskProgressStatus,
    WorkflowTaskProgressInput,
)

__all__ = [
    "FileSession",
    "SQLiteSession",
    "SessionBootstrap",
    "SessionTaskProgressManager",
    "TASK_PROGRESS_MAX_DESC_LENGTH",
    "TASK_PROGRESS_MAX_ERROR_LENGTH",
    "TASK_PROGRESS_MAX_ID_LENGTH",
    "TASK_PROGRESS_MAX_ITEMS",
    "TaskProgressCounts",
    "TaskProgressItem",
    "TaskProgressSnapshot",
    "TaskProgressSource",
    "TaskProgressStatus",
    "SessionSummary",
    "ValidationResult",
    "WorkflowTaskProgressInput",
    "build_session",
    "discover_file_sessions",
    "discover_sqlite_sessions",
    "find_session_by_id",
    "most_recent_session",
]
