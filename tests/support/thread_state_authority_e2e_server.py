"""Web thread 状态与审批双浏览器 E2E 专用后端。

功能：在隔离临时目录装配真实 FastAPI、ThreadManager、FileSession、
ThreadStatusManager 与 ApprovalInboxBroadcaster，并提供只用于测试驱动状态变更的
控制路由。

关键函数：
- ``_seed_thread``：写入一个可由真实 Web UI 打开的 generic_chat thread。
- ``_register_control_routes``：通过真实 Manager 发布 run 状态和审批事件。
- ``main``：装配 Web 应用并在 127.0.0.1:8080 启动。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from web_thread_fork_e2e_server import (
    _FileRuntime,
    _write_model_catalog,
)

from core.contracts import ApprovalDecision
from hosts.shared.host_dispatcher import HostDispatcher
from hosts.web.app import create_app
from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.approvals.global_inbox import get_inbox_broadcaster
from hosts.web.auth.secrets import hash_password
from hosts.web.threads.manager import ThreadManager
from hosts.web.threads.metadata import ThreadMetadata, write_thread_metadata
from hosts.web.websocket.thread_status import get_thread_status_manager
from hosts.web.websocket.thread_status_manager import ThreadStatusRunLease
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config
from safety.approval.manager import ApprovalManager
from safety.approval.permissions_manager import PermissionsManager
from safety.inbox.event_sink import InboxEventSink

PASSWORD = "thread-state-e2e-pwd"
THREAD_ID = "thread-cccccccccccc"
THREAD_NAME = "Thread State Authority E2E"


def _seed_thread(home: Path, workspace: Path) -> None:
    """写入真实 ThreadManager 可发现的 generic_chat metadata。"""
    now = time.time()
    write_thread_metadata(
        home,
        ThreadMetadata(
            id=THREAD_ID,
            name=THREAD_NAME,
            preset_id="preset-a",
            backend_kind="generic_chat",
            cwd=str(workspace),
            created_at=now,
            updated_at=now,
            message_count=0,
        ),
    )


def _config() -> Config:
    """构造关闭 scheduler、启用 Web dev mode 的隔离配置。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "preset-a"},
            "session": {
                "backend": "file",
                "file_store_path": ".kongming/sessions",
            },
            "web": {
                "enabled": True,
                "dev_mode": True,
                "host": "127.0.0.1",
                "port": 8080,
                "server_origin": "http://127.0.0.1:5174",
                "idle_timeout_seconds": 60,
                "idle_check_interval_seconds": 10,
            },
            "scheduler": {"enabled": False},
        }
    )


def _register_control_routes(
    app: FastAPI,
    workspace: Path,
    approval_manager: ApprovalManager,
) -> None:
    """注册驱动真实 Manager 的测试控制路由并记录并发审批结果。"""
    status_manager = get_thread_status_manager()
    inbox = get_inbox_broadcaster()
    leases: dict[str, ThreadStatusRunLease] = {}
    resolution_attempts = 0
    accepted_resolutions = 0
    tool_continuations = 0
    latest_outcome: str | None = None
    approval_task: asyncio.Task[ApprovalDecision] | None = None
    resolution_barrier = asyncio.Event()

    async def _counted_resolve(
        thread_id: str,
        request_id: str,
        decision: dict[str, Any],
    ) -> bool:
        """只记录输入输出，真实并发仲裁交给 ApprovalManager.resolve。"""
        nonlocal resolution_attempts, accepted_resolutions
        resolution_attempts += 1
        if resolution_attempts >= 2:
            resolution_barrier.set()
        await resolution_barrier.wait()
        accepted = await approval_manager.resolve(thread_id, request_id, decision)
        if accepted:
            accepted_resolutions += 1
        return accepted

    def _record_approval_result(task: asyncio.Task[ApprovalDecision]) -> None:
        """记录真实审批终态，并模拟只有 approved 才继续一次工具执行。"""
        nonlocal latest_outcome, tool_continuations
        decision = task.result()
        latest_outcome = decision.outcome
        if decision.approved:
            tool_continuations += 1

    @app.post("/__e2e/thread-state/start")
    async def start_run() -> dict[str, Any]:
        """发布 run-1 responding，让已连接和后连接浏览器分别消费 delta/snapshot。"""
        lease = await status_manager.begin_run(THREAD_ID, "run-1")
        leases["run-1"] = lease
        await status_manager.publish_status(lease, phase="responding")
        return {"ok": True, "sequence": status_manager.sequence}

    @app.post("/__e2e/thread-state/replace")
    async def replace_run() -> dict[str, Any]:
        """启动 run-2 后发送 run-1 迟到终态，验证 lease 拒绝旧 run。"""
        old_lease = leases["run-1"]
        new_lease = await status_manager.begin_run(THREAD_ID, "run-2")
        leases["run-2"] = new_lease
        await status_manager.publish_status(new_lease, phase="responding")
        stale_accepted = await status_manager.publish_status(old_lease, phase="complete")
        return {
            "ok": True,
            "stale_accepted": stale_accepted,
            "sequence": status_manager.sequence,
        }

    @app.post("/__e2e/thread-state/complete")
    async def complete_run() -> dict[str, Any]:
        """发布当前 run-2 终态并清理 active snapshot。"""
        accepted = await status_manager.publish_status(
            leases["run-2"],
            phase="complete",
        )
        return {"ok": accepted, "sequence": status_manager.sequence}

    @app.get("/__e2e/thread-state")
    async def thread_state() -> dict[str, Any]:
        """返回 Manager 的只读状态，供浏览器断言服务端最终事实。"""
        return {
            "active": {
                key: frame.model_dump() for key, frame in status_manager.active_statuses.items()
            },
            "sequence": status_manager.sequence,
            "connections": status_manager.connection_count,
        }

    @app.post("/__e2e/approval/add")
    async def add_approval() -> dict[str, Any]:
        """通过真实 ApprovalManager + InboxEventSink 创建 pending 卡片。"""
        nonlocal approval_task
        nonlocal accepted_resolutions, latest_outcome, resolution_attempts
        nonlocal tool_continuations
        resolution_attempts = 0
        accepted_resolutions = 0
        tool_continuations = 0
        latest_outcome = None
        resolution_barrier.clear()
        approval_task = asyncio.create_task(
            approval_manager.request(
                channel="generic_chat",
                thread_id=THREAD_ID,
                cwd=str(workspace),
                tool_name="run_shell",
                tool_input={"command": "printf e2e"},
                timeout_ms=10_000,
            ),
            name="thread-state-e2e-approval",
        )
        approval_task.add_done_callback(_record_approval_result)
        async with asyncio.timeout(2):
            while approval_manager.pending_count != 1 or inbox.pending_count != 1:
                await asyncio.sleep(0)
        request_id = inbox.pending_request_ids[0]
        inbox.register_resolve_target(THREAD_ID, _counted_resolve)
        return {"ok": True, "request_id": request_id}

    @app.get("/__e2e/approval")
    async def approval_state() -> dict[str, Any]:
        """返回 pending 与并发决议计数。"""
        return {
            "pending": approval_manager.pending_count,
            "inbox_pending": inbox.pending_count,
            "resolution_attempts": resolution_attempts,
            "accepted_resolutions": accepted_resolutions,
            "outcome": latest_outcome,
            "tool_continuations": tool_continuations,
        }

    # ``create_app`` 已注册 SPA catch-all；测试控制路由需要排在它之前才能命中。
    control_routes = [
        route for route in app.router.routes if getattr(route, "path", "").startswith("/__e2e/")
    ]
    app.router.routes[:] = control_routes + [
        route for route in app.router.routes if route not in control_routes
    ]


def main() -> None:
    """装配隔离应用并启动 Playwright 可访问的 uvicorn 服务。"""
    temporary_home = tempfile.TemporaryDirectory(prefix="kongming-thread-state-e2e-")
    workspace = Path(temporary_home.name)
    home = workspace / ".kongming"
    home.mkdir(parents=True, exist_ok=True)
    _write_model_catalog(home)
    web_dir = home / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / "password.hash").write_text(
        hash_password(PASSWORD),
        encoding="utf-8",
    )
    _seed_thread(home, workspace)
    config = _config()
    model_catalog_manager = ModelCatalogManager(user_path=home / "model-providers.yaml")
    session_store = home / "sessions"

    async def _runtime_factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        event_sinks: list[Any],
    ) -> tuple[Any, Any]:
        """装配真实 FileSession runtime，并把外部模型边界固定为 fake。"""
        del preset_id, adapter, event_sinks
        runtime = _FileRuntime(session_store, workspace)
        return runtime, HostDispatcher(runtime=runtime, session_id=thread_id)  # type: ignore[arg-type]

    manager = ThreadManager(
        config,
        kongming_home=home,
        runtime_factory=_runtime_factory,
        model_catalog_manager=model_catalog_manager,
    )
    app = create_app(
        config,
        manager,
        home_dir=home,
        model_catalog_manager=model_catalog_manager,
        lifespan_shutdown_timeout=1.0,
    )
    inbox = get_inbox_broadcaster()
    approval_manager = ApprovalManager(
        permissions_manager=PermissionsManager(home),
        default_timeout_ms=10_000,
    )
    approval_manager.register_event_sink(
        InboxEventSink(
            broadcaster=inbox,
            manager=approval_manager,
        )
    )
    _register_control_routes(app, workspace, approval_manager)
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")


if __name__ == "__main__":
    main()
