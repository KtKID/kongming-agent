"""task progress 单一 owner 浏览器 E2E 专用后端。

功能：在隔离目录装配真实 FastAPI、ThreadManager、SessionTaskProgressManager 与
workflow history artifact；测试控制路由把 fake LLM 的 start/next 意图交给真实
Manager，Vite 代理后的浏览器只读取产品 REST 接口。

关键流程：_seed_thread 写入可登录 thread，_register_control_routes 驱动 A/B
workflow 的真实状态机，main 启动供 Playwright 访问的 uvicorn。
关键函数：_open_workflow 创建 foreground，_write_workflow_history 写历史 owner。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from web_thread_fork_e2e_server import _FileRuntime, _write_model_catalog

from hosts.shared.host_dispatcher import HostDispatcher
from hosts.web.app import create_app
from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.auth.secrets import hash_password
from hosts.web.threads.manager import ThreadManager
from hosts.web.threads.metadata import ThreadMetadata, write_thread_metadata
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.models import Config
from sessions import (
    SessionTaskProgressManager,
    TaskProgressConflictError,
    TaskProgressControlMode,
    TaskProgressTaskDefinition,
)

PASSWORD = "task-progress-e2e-pwd"
THREAD_ID = "thread-dddddddddddd"
WORKFLOW_A_ID = "wf-task-progress-a"
WORKFLOW_B_ID = "wf-task-progress-b"
PROGRESS_PATH_LOCATOR_ENV = "TASK_PROGRESS_E2E_PROGRESS_PATH_LOCATOR"


def _config(session_store: Path) -> Config:
    """构造隔离 Web 配置，输入为 session 目录，输出为可启动 Config。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "preset-a"},
            "session": {"backend": "file", "file_store_path": str(session_store)},
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


def _seed_thread(home: Path, workspace: Path) -> None:
    """写入可由 ThreadManager 发现的 generic_chat thread，输入为 home/workspace。"""
    now = time.time()
    write_thread_metadata(
        home,
        ThreadMetadata(
            id=THREAD_ID,
            name="Task Progress Single Owner E2E",
            preset_id="preset-a",
            backend_kind="generic_chat",
            cwd=str(workspace),
            created_at=now,
            updated_at=now,
            message_count=0,
        ),
    )


def _workflow_tasks(prefix: str) -> list[TaskProgressTaskDefinition]:
    """构造两步 task-flow 骨架，输入为任务前缀，输出为冻结任务定义列表。"""
    return [
        TaskProgressTaskDefinition(
            task_id=f"{prefix}-step-1",
            task_run_id=f"001-{prefix}-step-1",
            desc="规划任务" if prefix == "a" else "复核发布",
            display_order=0,
        ),
        TaskProgressTaskDefinition(
            task_id=f"{prefix}-step-2",
            task_run_id=f"002-{prefix}-step-2",
            desc="执行任务" if prefix == "a" else "提交发布",
            depends_on=(f"{prefix}-step-1",),
            display_order=1,
        ),
    ]


def _write_workflow_history(
    session_store: Path,
    *,
    workflow_id: str,
    title: str,
    status: str,
) -> None:
    """写入 workflow history owner 的最小 artifact，输入为坐标与状态，输出为 workflow.json。"""
    workflow_dir = session_store / THREAD_ID / "agent-workflows" / workflow_id
    workflow_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "workflow_id": workflow_id,
        "mode": "task_flow",
        "parent_session_id": THREAD_ID,
        "started_at": "2026-08-01T00:00:00Z",
        "finished_at": "2026-08-01T00:00:01Z" if status == "completed" else None,
        "desc": title,
        "status": status,
        "assigned_agents": [],
    }
    (workflow_dir / "workflow.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _open_workflow(
    progress: SessionTaskProgressManager,
    *,
    workflow_id: str,
    title: str,
    prefix: str,
) -> None:
    """通过真实 Manager 创建 foreground，输入为 workflow 坐标，输出为全 pending 快照。"""
    progress.open_workflow(
        session_id=THREAD_ID,
        workflow_id=workflow_id,
        title=title,
        control_mode=TaskProgressControlMode.LLM_STEPS,
        tasks=_workflow_tasks(prefix),
    )


def _write_progress_path_locator(session_store: Path) -> None:
    """写入真实 progress 文件定位信息，输入为 session 根目录，输出为测试可读取的路径记录。"""
    raw_path = os.environ.get(PROGRESS_PATH_LOCATOR_ENV)
    if not raw_path:
        return
    locator_path = Path(raw_path)
    locator_path.write_text(
        json.dumps({"progress_path": str(session_store / THREAD_ID / "task_progress.json")}),
        encoding="utf-8",
    )


def _register_control_routes(
    app: FastAPI,
    *,
    progress: SessionTaskProgressManager,
    session_store: Path,
) -> None:
    """注册 fake LLM 控制路由，输入为 app/Manager/session 路径，输出为产品状态机变更。"""

    @app.post("/__e2e/task-progress/start-a")
    async def start_a() -> dict[str, object]:
        """模拟 LLM start A 第一步，输出为真实 Manager 当前快照。"""
        snapshot = progress.start_llm_step(THREAD_ID, WORKFLOW_A_ID, "a-step-1")
        return snapshot.model_dump(mode="json")

    @app.post("/__e2e/task-progress/next-a")
    async def next_a() -> dict[str, object]:
        """模拟 LLM next A，输出为原子完成第一步并启动第二步的快照。"""
        snapshot = progress.advance_llm_step(
            THREAD_ID,
            WORKFLOW_A_ID,
            "a-step-1",
            "a-step-2",
        )
        return snapshot.model_dump(mode="json")

    @app.post("/__e2e/task-progress/complete-a")
    async def complete_a() -> dict[str, object]:
        """模拟 LLM 最后一次 next，输出为 A 的 completed 快照。"""
        snapshot = progress.advance_llm_step(THREAD_ID, WORKFLOW_A_ID, "a-step-2", None)
        _write_workflow_history(
            session_store,
            workflow_id=WORKFLOW_A_ID,
            title="A 计划",
            status="completed",
        )
        return snapshot.model_dump(mode="json")

    @app.post("/__e2e/task-progress/open-b-and-reject-late-a")
    async def open_b_and_reject_late_a() -> dict[str, object]:
        """创建 B 并提交 A 运行中的晚到 next，输出为前置状态与旧命令拒绝事实。"""
        before_takeover = progress.read_snapshot(THREAD_ID)
        _open_workflow(
            progress,
            workflow_id=WORKFLOW_B_ID,
            title="B 计划",
            prefix="b",
        )
        started = progress.start_llm_step(THREAD_ID, WORKFLOW_B_ID, "b-step-1")
        late_a_rejected = False
        try:
            progress.advance_llm_step(THREAD_ID, WORKFLOW_A_ID, "a-step-2", None)
        except TaskProgressConflictError:
            late_a_rejected = True
        _write_workflow_history(
            session_store,
            workflow_id=WORKFLOW_B_ID,
            title="B 计划",
            status="running",
        )
        return {
            "a_before_takeover": before_takeover.model_dump(mode="json"),
            "late_a_action": "next",
            "late_a_rejected": late_a_rejected,
            "snapshot": started.model_dump(mode="json"),
        }

    @app.post("/__e2e/task-progress/next-b")
    async def next_b() -> dict[str, object]:
        """模拟 LLM next B，输出为 B 第一步完成并启动第二步的快照。"""
        snapshot = progress.advance_llm_step(
            THREAD_ID,
            WORKFLOW_B_ID,
            "b-step-1",
            "b-step-2",
        )
        return snapshot.model_dump(mode="json")

    @app.post("/__e2e/task-progress/complete-b")
    async def complete_b() -> dict[str, object]:
        """模拟 LLM 最后一次 next B，输出为 B completed 快照与完成历史。"""
        snapshot = progress.advance_llm_step(THREAD_ID, WORKFLOW_B_ID, "b-step-2", None)
        _write_workflow_history(
            session_store,
            workflow_id=WORKFLOW_B_ID,
            title="B 计划",
            status="completed",
        )
        return snapshot.model_dump(mode="json")

    @app.get("/__e2e/task-progress")
    async def task_progress_state() -> dict[str, object]:
        """返回真实 Manager 当前快照，输入为空，输出为只读调试事实。"""
        return progress.read_snapshot(THREAD_ID).model_dump(mode="json")

    control_routes = [
        route for route in app.router.routes if getattr(route, "path", "").startswith("/__e2e/")
    ]
    app.router.routes[:] = control_routes + [
        route for route in app.router.routes if route not in control_routes
    ]


def main() -> None:
    """装配隔离真实 Web 端到端环境，输入为空，输出为运行中的 uvicorn 服务。"""
    temporary_home = tempfile.TemporaryDirectory(prefix="kongming-task-progress-e2e-")
    workspace = Path(temporary_home.name)
    home = workspace / ".kongming"
    home.mkdir(parents=True, exist_ok=True)
    _write_model_catalog(home)
    web_dir = home / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / "password.hash").write_text(hash_password(PASSWORD), encoding="utf-8")
    _seed_thread(home, workspace)
    session_store = home / "sessions"
    config = _config(session_store)
    model_catalog_manager = ModelCatalogManager(user_path=home / "model-providers.yaml")
    progress = SessionTaskProgressManager.from_config(config)
    _open_workflow(progress, workflow_id=WORKFLOW_A_ID, title="A 计划", prefix="a")
    _write_progress_path_locator(session_store)
    _write_workflow_history(
        session_store,
        workflow_id=WORKFLOW_A_ID,
        title="A 计划",
        status="running",
    )

    async def _runtime_factory(
        thread_id: str,
        preset_id: str,
        adapter: WebHostAdapter,
        event_sinks: list[Any],
    ) -> tuple[Any, Any]:
        """装配只替换外部 LLM 的 FileSession runtime，输出为 runtime/dispatcher。"""
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
        task_progress_manager=progress,
        lifespan_shutdown_timeout=1.0,
    )
    _register_control_routes(app, progress=progress, session_store=session_store)
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")


if __name__ == "__main__":
    main()
