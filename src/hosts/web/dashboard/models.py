"""dashboard 领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class DashboardPollingConfig:
    interval_seconds: int = 5

    @property
    def normalized_interval_seconds(self) -> int:
        return max(self.interval_seconds, 3)


@dataclass(frozen=True)
class ProcessStatus:
    running: bool
    pid: int
    host: str
    port: int
    url: str
    log_path: str


@dataclass(frozen=True)
class GlobalWSCounters:
    thread_status_connections: int
    cron_connections: int
    approval_subscribers: int


@dataclass(frozen=True)
class ProviderSessionCounters:
    claude_active_sessions: int
    codex_active_sessions: int


@dataclass(frozen=True)
class ActiveCellSnapshot:
    thread_id: str
    thread_name: str
    backend_kind: Literal["generic_chat", "claude_code", "codex"]
    preset_id: str
    cwd: str
    created_at: float
    last_active_at: float
    pending_approval_count: int
    status: Literal["idle", "running", "awaiting_approval"]
    chat_ws_connections: int


@dataclass(frozen=True)
class RuntimeStatusSnapshot:
    process: ProcessStatus
    polling: DashboardPollingConfig
    global_ws: GlobalWSCounters
    provider_sessions: ProviderSessionCounters
    cells_total: int
    chat_ws_connections_total: int
    approval_pending_total: int
    workspace_shell_connections: int | None
    cells: list[ActiveCellSnapshot]
    generated_at_ms: int
