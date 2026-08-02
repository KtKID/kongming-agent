"""Thread task progress 只读 REST router 测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hosts.web.app import create_app
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.protocol import TaskProgressSnapshotPayload
from hosts.web.threads.metadata import ThreadMetadata
from infrastructure.config.models import Config
from sessions import (
    SessionTaskProgressManager,
    TaskProgressControlMode,
    TaskProgressTaskDefinition,
)
from tests.unit.test_web_app_lifespan import _seed_password

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


class FakeTM:
    """满足 task-progress router 所需 ThreadManager 合同。"""

    def __init__(self, threads: list[ThreadMetadata] | None = None) -> None:
        """保存可查询 thread，输入为元数据列表，输出为空。"""
        self._threads = {item.id: item for item in (threads or [])}
        self.usage_manager = object()

    async def start(self) -> None:
        """满足应用 lifespan，输入为空，输出为空。"""

    async def aclose_all(self) -> None:
        """满足应用 shutdown，输入为空，输出为空。"""

    def list_threads(self) -> list[ThreadMetadata]:
        """返回已注册 thread，输入为空，输出为元数据列表。"""
        return list(self._threads.values())

    def __getattr__(self, name: str) -> Any:
        """拒绝本路由未使用的 ThreadManager 方法，输入为属性名，输出为明确错误。"""
        raise AttributeError(name)


def _meta(thread_id: str) -> ThreadMetadata:
    """构造 thread 元数据，输入为 ID，输出为可查询元数据。"""
    return ThreadMetadata(
        id=thread_id,
        name="t",
        preset_id="p",
        backend_kind="generic_chat",
        cwd="",
        created_at=1.0,
        updated_at=2.0,
        message_count=0,
    )


def _cfg(session_root: Path) -> Config:
    """构造 Web/file-session 配置，输入为 session 根路径，输出为 Config。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "local-gemma-4-e4b-it"},
            "session": {"backend": "file", "file_store_path": str(session_root)},
            "web": {"enabled": True, "dev_mode": True},
        }
    )


def _login_client(tmp_path: Path, tm: FakeTM) -> TestClient:
    """创建已登录 TestClient，输入为临时路径与 ThreadManager，输出为活动客户端。"""
    _seed_password(tmp_path, "pwd")
    cfg = _cfg(tmp_path / "sessions")
    app = create_app(
        cfg,
        tm,
        home_dir=tmp_path,
        task_progress_manager=SessionTaskProgressManager.from_config(cfg),
    )
    client = TestClient(app)
    client.__enter__()
    response = client.post(
        "/api/auth/login",
        json={"password": "pwd"},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200
    return client


def _seed_foreground(tmp_path: Path, thread_id: str) -> None:
    """落盘 v2 前台 workflow，输入为临时路径与 thread ID，输出为当前快照。"""
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))
    manager.open_workflow(
        session_id=thread_id,
        workflow_id="wf-1",
        title="发布检查",
        control_mode=TaskProgressControlMode.LLM_STEPS,
        tasks=[
            TaskProgressTaskDefinition(
                task_id="step-1",
                task_run_id="001-step-1",
                desc="检查发布状态",
                display_order=0,
            )
        ],
    )


def test_get_missing_file_returns_v2_empty_snapshot(tmp_path: Path) -> None:
    """缺失快照经真实 HTTP GET 返回 v2 空投影。"""
    thread_id = "thread-abc123abc123"
    client = _login_client(tmp_path, FakeTM([_meta(thread_id)]))
    try:
        response = client.get(f"/api/threads/{thread_id}/task-progress")
        assert response.status_code == 200
        assert response.json() == {
            "schema_version": 2,
            "session_id": thread_id,
            "workflow_id": None,
            "title": None,
            "control_mode": None,
            "updated_at_ms": response.json()["updated_at_ms"],
            "tasks": [],
            "counts": {
                "pending": 0,
                "in_progress": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "total": 0,
            },
        }
    finally:
        client.__exit__(None, None, None)


def test_get_returns_v2_foreground_through_protocol_model(tmp_path: Path) -> None:
    """真实 Manager 快照经 router 协议模型返回，字段与前端真源一致。"""
    thread_id = "thread-abc123abc123"
    _seed_foreground(tmp_path, thread_id)
    client = _login_client(tmp_path, FakeTM([_meta(thread_id)]))
    try:
        response = client.get(f"/api/threads/{thread_id}/task-progress")
        assert response.status_code == 200
        body = response.json()
        assert body["workflow_id"] == "wf-1"
        assert body["title"] == "发布检查"
        assert body["control_mode"] == "llm_steps"
        assert body["tasks"] == [
            {
                "task_id": "step-1",
                "task_run_id": "001-step-1",
                "desc": "检查发布状态",
                "depends_on": [],
                "status": "pending",
                "display_order": 0,
                "error_message": None,
                "updated_at_ms": body["tasks"][0]["updated_at_ms"],
            }
        ]
        assert body["counts"]["failed"] == 0
        assert "source" not in body
        assert "orchestration_task_id" not in body["tasks"][0]
    finally:
        client.__exit__(None, None, None)


def test_put_route_is_absent(tmp_path: Path) -> None:
    """Web 只提供读取入口，HTTP PUT 无法绕过状态机写入。"""
    thread_id = "thread-abc123abc123"
    client = _login_client(tmp_path, FakeTM([_meta(thread_id)]))
    try:
        response = client.put(
            f"/api/threads/{thread_id}/task-progress",
            json={"tasks": []},
            headers=CSRF_HEADERS,
        )
        assert response.status_code == 405
        assert not (tmp_path / "sessions" / thread_id / "task_progress.json").exists()
    finally:
        client.__exit__(None, None, None)


def test_get_rejects_corrupted_or_v1_snapshot(tmp_path: Path) -> None:
    """损坏 JSON 与旧 schema 都在 router 边界以客户端错误返回。"""
    thread_id = "thread-abc123abc123"
    path = tmp_path / "sessions" / thread_id / "task_progress.json"
    path.parent.mkdir(parents=True)
    client = _login_client(tmp_path, FakeTM([_meta(thread_id)]))
    try:
        path.write_text("{bad json", encoding="utf-8")
        corrupted = client.get(f"/api/threads/{thread_id}/task-progress")
        path.write_text(
            json.dumps({"schema_version": 1, "session_id": thread_id, "tasks": []}),
            encoding="utf-8",
        )
        legacy = client.get(f"/api/threads/{thread_id}/task-progress")
        assert corrupted.status_code == 422
        assert legacy.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_protocol_payload_rejects_unknown_and_missing_v2_fields() -> None:
    """Python wire 真源拒绝 schema 漂移字段与缺失计数。"""
    payload = {
        "schema_version": 2,
        "session_id": "thread-abc123abc123",
        "workflow_id": None,
        "title": None,
        "control_mode": None,
        "updated_at_ms": 1,
        "tasks": [],
        "counts": {
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
            "total": 0,
        },
    }

    assert TaskProgressSnapshotPayload.model_validate(payload).schema_version == 2
    with pytest.raises(ValidationError, match="extra_forbidden"):
        TaskProgressSnapshotPayload.model_validate({**payload, "source": "workflow"})
    missing_counts = {**payload, "counts": {"total": 0}}
    with pytest.raises(ValidationError, match="Field required"):
        TaskProgressSnapshotPayload.model_validate(missing_counts)


def test_get_requires_valid_existing_thread(tmp_path: Path) -> None:
    """路由在读取任何状态文件前校验 thread 格式和存在性。"""
    client = _login_client(tmp_path, FakeTM([]))
    try:
        missing = client.get("/api/threads/thread-abc123abc123/task-progress")
        malformed = client.get("/api/threads/not-a-thread/task-progress")
        assert missing.status_code == 404
        assert malformed.status_code == 422
    finally:
        client.__exit__(None, None, None)
