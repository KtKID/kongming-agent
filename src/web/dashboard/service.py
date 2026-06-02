"""dashboard runtime snapshot 聚合服务。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from config_loader.models import Config
from web.dashboard.models import (
    ActiveCellSnapshot,
    DashboardPollingConfig,
    GlobalWSCounters,
    ProcessStatus,
    ProviderSessionCounters,
    RuntimeStatusSnapshot,
)
from web.dashboard.network import (
    get_active_session_count,
    get_approval_pending,
    get_approval_subscribers,
    get_cell_chat_ws_connections,
    get_cron_connections,
    get_thread_status_connections,
)
from web.protocol import (
    ActiveCellStatusDTO,
    RuntimeStatusGlobalWSDTO,
    RuntimeStatusPollingDTO,
    RuntimeStatusProcessDTO,
    RuntimeStatusProviderSessionsDTO,
    RuntimeStatusSnapshotDTO,
)
from web.types import ThreadManagerProtocol


class RuntimeStatusService:
    def __init__(
        self,
        cfg: Config,
        thread_manager: ThreadManagerProtocol,
        *,
        claude_session_manager: Any,
        codex_session_manager: Any,
        approval_inbox_broadcaster: Any,
        kongming_home: Path,
    ) -> None:
        self._cfg = cfg
        self._thread_manager = thread_manager
        self._claude_session_manager = claude_session_manager
        self._codex_session_manager = codex_session_manager
        self._approval_inbox_broadcaster = approval_inbox_broadcaster
        self._kongming_home = kongming_home

    def build_snapshot(self) -> RuntimeStatusSnapshot:
        polling = DashboardPollingConfig(
            interval_seconds=self._cfg.web.dashboard_poll_interval_seconds
        )
        cell_summaries = self._thread_manager.list_cells()
        cells: list[ActiveCellSnapshot] = []
        for summary in sorted(cell_summaries, key=lambda item: item.last_active_at, reverse=True):
            cell = self._thread_manager.get_cell(summary.thread_id)
            if cell is None:
                continue
            metadata = getattr(cell, "metadata", None)
            cells.append(
                ActiveCellSnapshot(
                    thread_id=summary.thread_id,
                    thread_name=summary.thread_name,
                    backend_kind=getattr(metadata, "backend_kind", "generic_chat"),
                    preset_id=summary.preset_id,
                    cwd=getattr(metadata, "cwd", "") or "",
                    created_at=summary.created_at,
                    last_active_at=summary.last_active_at,
                    pending_approval_count=summary.pending_approval_count,
                    status=summary.status,
                    chat_ws_connections=get_cell_chat_ws_connections(cell),
                )
            )

        return RuntimeStatusSnapshot(
            process=ProcessStatus(
                running=True,
                pid=os.getpid(),
                host=self._cfg.web.host,
                port=self._cfg.web.port,
                url=f"http://localhost:{self._cfg.web.port}",
                log_path=str(self._kongming_home / "web" / "server.log"),
            ),
            polling=polling,
            global_ws=GlobalWSCounters(
                thread_status_connections=get_thread_status_connections(),
                cron_connections=get_cron_connections(),
                approval_subscribers=get_approval_subscribers(self._approval_inbox_broadcaster),
            ),
            provider_sessions=ProviderSessionCounters(
                claude_active_sessions=get_active_session_count(self._claude_session_manager),
                codex_active_sessions=get_active_session_count(self._codex_session_manager),
            ),
            cells_total=len(cells),
            chat_ws_connections_total=sum(cell.chat_ws_connections for cell in cells),
            approval_pending_total=get_approval_pending(self._approval_inbox_broadcaster),
            workspace_shell_connections=None,
            cells=cells,
            generated_at_ms=int(time.time() * 1000),
        )

    def build_snapshot_dto(self) -> RuntimeStatusSnapshotDTO:
        snapshot = self.build_snapshot()
        return RuntimeStatusSnapshotDTO(
            process=RuntimeStatusProcessDTO(
                running=snapshot.process.running,
                pid=snapshot.process.pid,
                host=snapshot.process.host,
                port=snapshot.process.port,
                url=snapshot.process.url,
                log_path=snapshot.process.log_path,
            ),
            polling=RuntimeStatusPollingDTO(
                interval_seconds=snapshot.polling.normalized_interval_seconds
            ),
            global_ws=RuntimeStatusGlobalWSDTO(
                thread_status_connections=snapshot.global_ws.thread_status_connections,
                cron_connections=snapshot.global_ws.cron_connections,
                approval_subscribers=snapshot.global_ws.approval_subscribers,
            ),
            provider_sessions=RuntimeStatusProviderSessionsDTO(
                claude_active_sessions=snapshot.provider_sessions.claude_active_sessions,
                codex_active_sessions=snapshot.provider_sessions.codex_active_sessions,
            ),
            cells_total=snapshot.cells_total,
            chat_ws_connections_total=snapshot.chat_ws_connections_total,
            approval_pending_total=snapshot.approval_pending_total,
            workspace_shell_connections=snapshot.workspace_shell_connections,
            cells=[
                ActiveCellStatusDTO(
                    thread_id=cell.thread_id,
                    thread_name=cell.thread_name,
                    backend_kind=cell.backend_kind,
                    preset_id=cell.preset_id,
                    cwd=cell.cwd,
                    created_at=cell.created_at,
                    last_active_at=cell.last_active_at,
                    pending_approval_count=cell.pending_approval_count,
                    status=cell.status,
                    chat_ws_connections=cell.chat_ws_connections,
                )
                for cell in snapshot.cells
            ],
            generated_at_ms=snapshot.generated_at_ms,
        )
