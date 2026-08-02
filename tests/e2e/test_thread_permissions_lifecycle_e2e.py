"""Thread permissions 随 Web thread 生命周期清理与补偿的端到端测试。

关键流程通过真实 FastAPI TestClient、ThreadManager、REST 路由和 JSON store 完成：
创建 thread、保存本子、重命名与归档保留本子、删除主状态成功、清理失败进入
重试队列，随后显式补偿成功并产生失败/完成两类审计事件。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hosts.web.app import create_app
from hosts.web.app_support.host_adapter import WebHostAdapter
from hosts.web.auth.middleware import CSRF_HEADER_NAME, CSRF_HEADER_VALUE
from hosts.web.auth.secrets import hash_password
from hosts.web.threads.manager import ThreadManager
from hosts.web.threads.metadata import ThreadMetadata, write_thread_metadata
from infrastructure.config.models import Config
from safety.approval.permissions_errors import PermissionsStoreError
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.rule_models import PermissionRuleRecord

CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}


def _config() -> Config:
    """构造关闭 scheduler 的最小本地 Web 配置。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "local-gemma-4-e4b-it"},
            "web": {"enabled": True, "dev_mode": True},
            "scheduler": {"enabled": False},
        }
    )


async def _unused_runtime_factory(
    thread_id: str,
    preset_id: str,
    adapter: WebHostAdapter,
    event_sinks: list[Any],
) -> tuple[Any, Any]:
    """本用例只走 metadata 与 permissions；误启动聊天 runtime 时立即失败。"""
    del thread_id, preset_id, adapter, event_sinks
    raise AssertionError("runtime factory must not be called")


def _seed_password(home: Path, password: str) -> None:
    """写入测试登录密码 hash，使 TestClient 走真实认证中间件。"""
    web_dir = home / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / "password.hash").write_text(
        hash_password(password),
        encoding="utf-8",
    )


def _permissions_path(home: Path, thread_id: str) -> Path:
    """按公开散列路径合同计算 thread 本子文件。"""
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    return home / "safety" / "thread_permissions" / f"{digest}.json"


def _metadata(thread_id: str, preset_id: str) -> ThreadMetadata:
    """构造 lifespan 前已存在的 Web thread metadata。"""
    return ThreadMetadata(
        id=thread_id,
        name="permissions lifecycle",
        preset_id=preset_id,
        created_at=1.0,
        updated_at=2.0,
        message_count=0,
    )


async def _seed_books(home: Path, thread_ids: tuple[str, ...]) -> None:
    """通过真实 PermissionsManager 在 lifespan 前物化多宿主权限本。"""
    permissions = PermissionsManager(home)
    for thread_id in thread_ids:
        await permissions.replace(
            thread_id,
            allow=[PermissionRuleRecord(expression="read_file", scope_cwd=None)],
            deny=[],
            expected_revision=0,
        )


class _FlakyPermissionsManager(PermissionsManager):
    """前两次删除失败、第三次委托真实 store 删除的门户。"""

    def __init__(self, home: Path) -> None:
        """记录删除尝试次数并复用真实 JSON store。"""
        super().__init__(home)
        self.delete_attempts = 0

    async def delete_thread(self, thread_id: str) -> None:
        """注入初次清理和后台单次重试失败。"""
        self.delete_attempts += 1
        if self.delete_attempts <= 2:
            raise PermissionsStoreError("injected cleanup failure")
        await super().delete_thread(thread_id)


def _wait_for_background_retry(manager: _FlakyPermissionsManager) -> None:
    """等待 TestClient 事件循环完成第二次删除尝试。"""
    deadline = time.monotonic() + 2.0
    while manager.delete_attempts < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.delete_attempts == 2


async def test_lifespan_preserves_all_hosts_and_rest_delete_targets_web_only(
    tmp_path: Path,
) -> None:
    """真实 lifespan 保留三宿主本子，REST 删除只影响显式 Web thread。"""
    home = tmp_path / ".kongming"
    password = "test-password"
    _seed_password(home, password)
    cfg = _config()
    web_thread = "thread-aaaaaaaaaaaa"
    cli_thread = "thread-bbbbbbbbbbbb"
    cron_thread = "thread-cccccccccccc"
    write_thread_metadata(home, _metadata(web_thread, cfg.model.preset_id))
    await _seed_books(home, (web_thread, cli_thread, cron_thread))
    before = {
        thread_id: _permissions_path(home, thread_id).read_text(encoding="utf-8")
        for thread_id in (web_thread, cli_thread, cron_thread)
    }
    thread_manager = ThreadManager(
        cfg,
        kongming_home=home,
        runtime_factory=_unused_runtime_factory,
    )
    app = create_app(cfg, thread_manager, home_dir=home)

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"password": password},
            headers=CSRF_HEADERS,
        )
        assert login.status_code == 200
        for thread_id, raw in before.items():
            assert _permissions_path(home, thread_id).read_text(encoding="utf-8") == raw

        deleted = client.delete(
            f"/api/threads/{web_thread}",
            headers=CSRF_HEADERS,
        )
        assert deleted.status_code == 204
        assert _permissions_path(home, web_thread).exists() is False
        for thread_id in (cli_thread, cron_thread):
            assert (
                _permissions_path(home, thread_id).read_text(encoding="utf-8") == before[thread_id]
            )


def test_delete_cleanup_failure_keeps_thread_deleted_and_retry_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REST 删除先提交主状态，补偿重试最终清理本子并留下双份审计。"""
    home = tmp_path / ".kongming"
    password = "test-password"
    _seed_password(home, password)
    cfg = _config()
    thread_manager = ThreadManager(
        cfg,
        kongming_home=home,
        runtime_factory=_unused_runtime_factory,
    )
    app = create_app(cfg, thread_manager, home_dir=home)
    failed_events: list[tuple[str, str]] = []
    completed_events: list[tuple[str, str]] = []

    def _capture_failure(
        component: str,
        event: str,
        exc: BaseException,
        **fields: object,
    ) -> None:
        """收集本子清理失败事件及 thread 身份。"""
        del component, exc
        failed_events.append((event, str(fields.get("thread_id", ""))))

    def _capture_completed(component: str, event: str, **fields: object) -> None:
        """收集补偿成功事件及 thread 身份。"""
        del component
        completed_events.append((event, str(fields.get("thread_id", ""))))

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"password": password},
            headers=CSRF_HEADERS,
        )
        assert login.status_code == 200
        created = client.post(
            "/api/threads",
            json={"name": "permissions", "preset_id": cfg.model.preset_id},
            headers=CSRF_HEADERS,
        )
        assert created.status_code == 201, created.text
        thread_id = str(created.json()["id"])
        saved = client.put(
            f"/api/threads/{thread_id}/permissions",
            json={
                "thread_id": thread_id,
                "revision": 0,
                "allow": [{"expression": "read_file", "scope_cwd": None}],
                "deny": [],
            },
            headers=CSRF_HEADERS,
        )
        assert saved.status_code == 200
        book_path = _permissions_path(home, thread_id)
        assert book_path.exists()

        renamed = client.patch(
            f"/api/threads/{thread_id}",
            json={"name": "renamed"},
            headers=CSRF_HEADERS,
        )
        archived = client.patch(
            f"/api/threads/{thread_id}",
            json={"is_archived": True},
            headers=CSRF_HEADERS,
        )
        assert renamed.status_code == 200
        assert archived.status_code == 200
        assert book_path.exists()

        flaky = _FlakyPermissionsManager(home)
        thread_manager.set_permissions_manager(flaky)
        monkeypatch.setattr(
            "hosts.web.threads.manager.log_network_exception",
            _capture_failure,
        )
        monkeypatch.setattr(
            "hosts.web.threads.manager.log_network_event",
            _capture_completed,
        )

        deleted = client.delete(
            f"/api/threads/{thread_id}",
            headers=CSRF_HEADERS,
        )
        assert deleted.status_code == 204
        assert client.get(f"/api/threads/{thread_id}/permissions").status_code == 404
        _wait_for_background_retry(flaky)
        assert book_path.exists()
        assert thread_manager.pending_permissions_cleanup == (thread_id,)

        assert client.portal is not None
        completed = client.portal.call(thread_manager.retry_permissions_cleanup)
        assert completed == (thread_id,)
        assert book_path.exists() is False
        assert thread_manager.pending_permissions_cleanup == ()

    assert failed_events == [
        ("thread_permissions_cleanup_failed", thread_id),
        ("thread_permissions_cleanup_failed", thread_id),
    ]
    assert completed_events == [("thread_permissions_cleanup_completed", thread_id)]
