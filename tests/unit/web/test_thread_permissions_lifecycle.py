"""验证 ThreadManager 对 thread permissions 的显式删除与跨宿主隔离。

覆盖正常清理、清理失败不回滚 thread 主状态、显式重试、启动保留其他宿主
本子，以及重命名和归档保留本子。所有本子写入均走真实 PermissionsManager。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

from hosts.web.threads.manager import ThreadManager
from hosts.web.threads.metadata import (
    ThreadMetadata,
    read_thread_metadata,
    write_thread_metadata,
)
from infrastructure.config.models import Config
from safety.approval.permissions_errors import PermissionsStoreError
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.rule_models import PermissionRuleRecord


def _config() -> Config:
    """构造启用 Web 的最小本地配置。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "local-gemma-4-e4b-it"},
            "web": {
                "enabled": True,
                "idle_timeout_seconds": 60,
                "idle_check_interval_seconds": 10,
            },
        }
    )


def _metadata(thread_id: str) -> ThreadMetadata:
    """构造可落盘的 thread metadata。"""
    return ThreadMetadata(
        id=thread_id,
        name="permissions lifecycle",
        preset_id="p1",
        created_at=1.0,
        updated_at=2.0,
        message_count=0,
    )


async def _factory(*args: object, **kwargs: object) -> tuple[Any, Any]:
    """生命周期测试不会 boot runtime，误调用时直接失败。"""
    del args, kwargs
    raise AssertionError("runtime factory must not be called")


def _permissions_path(home: Path, thread_id: str) -> Path:
    """按公开落盘合同计算本子文件路径。"""
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    return home / "safety" / "thread_permissions" / f"{digest}.json"


class _FlakyPermissionsManager(PermissionsManager):
    """前两次删除失败、第三次成功的真实 Manager 测试替身。"""

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self.delete_attempts = 0

    async def delete_thread(self, thread_id: str) -> None:
        """注入两次存储失败，随后委托真实删除。"""
        self.delete_attempts += 1
        if self.delete_attempts <= 2:
            raise PermissionsStoreError("injected cleanup failure")
        await super().delete_thread(thread_id)


async def _seed_book(manager: PermissionsManager, thread_id: str) -> None:
    """为目标 thread 物化一条 allow。"""
    await manager.replace(
        thread_id,
        allow=[PermissionRuleRecord(expression="read_file", scope_cwd=None)],
        deny=[],
        expected_revision=0,
    )


async def test_delete_thread_removes_permissions_book(tmp_path: Path) -> None:
    """正常删除只清理目标本子，并保留 CLI、cron 冲突哨兵。"""
    thread_id = "thread-aaaaaaaaaaaa"
    cli_thread = "thread-cccccccccccc"
    cron_thread = "thread-dddddddddddd"
    write_thread_metadata(tmp_path, _metadata(thread_id))
    permissions = PermissionsManager(tmp_path)
    await _seed_book(permissions, thread_id)
    await _seed_book(permissions, cli_thread)
    await _seed_book(permissions, cron_thread)
    preserved = {
        other_id: await permissions.snapshot(other_id) for other_id in (cli_thread, cron_thread)
    }
    manager = ThreadManager(
        _config(),
        kongming_home=tmp_path,
        runtime_factory=_factory,
        permissions_manager=permissions,
    )

    await manager.delete_thread(thread_id)

    assert manager.list_threads() == []
    assert _permissions_path(tmp_path, thread_id).exists() is False
    assert manager.pending_permissions_cleanup == ()
    for other_id, snapshot in preserved.items():
        assert _permissions_path(tmp_path, other_id).exists() is True
        assert await permissions.snapshot(other_id) == snapshot


async def test_cleanup_failure_keeps_delete_committed_and_retry_succeeds(
    tmp_path: Path,
) -> None:
    """清理失败进入队列，thread 主删除保持成功，后续重试最终删本子。"""
    thread_id = "thread-bbbbbbbbbbbb"
    write_thread_metadata(tmp_path, _metadata(thread_id))
    permissions = _FlakyPermissionsManager(tmp_path)
    await _seed_book(permissions, thread_id)
    manager = ThreadManager(
        _config(),
        kongming_home=tmp_path,
        runtime_factory=_factory,
        permissions_manager=permissions,
    )

    await manager.delete_thread(thread_id)
    await asyncio.sleep(0.01)

    assert manager.list_threads() == []
    assert _permissions_path(tmp_path, thread_id).exists() is True
    assert manager.pending_permissions_cleanup == (thread_id,)

    assert await manager.retry_permissions_cleanup() == (thread_id,)
    assert _permissions_path(tmp_path, thread_id).exists() is False
    assert manager.pending_permissions_cleanup == ()


async def test_delete_missing_web_metadata_preserves_other_host_book(
    tmp_path: Path,
) -> None:
    """缺少 Web metadata 时直接删除保持其他宿主同 ID 权限本。"""
    thread_id = "thread-bbbbbbbbbbbb"
    permissions = PermissionsManager(tmp_path)
    await _seed_book(permissions, thread_id)
    expected = await permissions.snapshot(thread_id)
    manager = ThreadManager(
        _config(),
        kongming_home=tmp_path,
        runtime_factory=_factory,
        permissions_manager=permissions,
    )

    await manager.delete_thread(thread_id)

    assert _permissions_path(tmp_path, thread_id).exists() is True
    assert await permissions.snapshot(thread_id) == expected


async def test_metadata_delete_failure_preserves_permissions_book(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metadata 提交未达 absent 时中止权限清理并向调用方暴露失败。"""
    thread_id = "thread-cccccccccccc"
    write_thread_metadata(tmp_path, _metadata(thread_id))
    permissions = PermissionsManager(tmp_path)
    await _seed_book(permissions, thread_id)
    manager = ThreadManager(
        _config(),
        kongming_home=tmp_path,
        runtime_factory=_factory,
        permissions_manager=permissions,
    )
    monkeypatch.setattr(
        "hosts.web.threads.manager.delete_thread_metadata_dir",
        lambda home, target_id: None,
    )

    with pytest.raises(OSError, match="metadata delete did not commit"):
        await manager.delete_thread(thread_id)

    assert read_thread_metadata(tmp_path, thread_id) == _metadata(thread_id)
    assert _permissions_path(tmp_path, thread_id).exists() is True


async def test_stale_metadata_update_cannot_resurrect_deleted_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除提交后，已读取旧快照的并发更新无法复活 metadata。"""
    thread_id = "thread-dddddddddddd"
    write_thread_metadata(tmp_path, _metadata(thread_id))
    permissions = PermissionsManager(tmp_path)
    await _seed_book(permissions, thread_id)
    manager = ThreadManager(
        _config(),
        kongming_home=tmp_path,
        runtime_factory=_factory,
        permissions_manager=permissions,
    )
    update_ready = asyncio.Event()
    release_update = asyncio.Event()
    persist = manager._persist_thread_metadata

    async def _delay_stale_persist(meta: ThreadMetadata) -> ThreadMetadata:
        """冻结已读取旧快照的更新，等待删除提交后再继续。"""
        update_ready.set()
        await release_update.wait()
        return await persist(meta)

    monkeypatch.setattr(manager, "_persist_thread_metadata", _delay_stale_persist)
    rename_task = asyncio.create_task(manager.rename_thread(thread_id, "stale rename"))
    await update_ready.wait()
    await manager.delete_thread(thread_id)
    release_update.set()

    with pytest.raises(KeyError, match="thread not found"):
        await rename_task
    assert read_thread_metadata(tmp_path, thread_id) is None
    assert _permissions_path(tmp_path, thread_id).exists() is False


async def test_start_preserves_permission_books_from_every_host(
    tmp_path: Path,
) -> None:
    """Web 启动只读取本宿主 metadata，并保留 Web、CLI、cron 三类本子。"""
    web_thread = "thread-cccccccccccc"
    cli_thread = "thread-dddddddddddd"
    cron_thread = "thread-eeeeeeeeeeee"
    write_thread_metadata(tmp_path, _metadata(web_thread))
    permissions = PermissionsManager(tmp_path)
    await _seed_book(permissions, web_thread)
    await _seed_book(permissions, cli_thread)
    await _seed_book(permissions, cron_thread)
    expected = {
        thread_id: await permissions.snapshot(thread_id)
        for thread_id in (web_thread, cli_thread, cron_thread)
    }
    manager = ThreadManager(
        _config(),
        kongming_home=tmp_path,
        runtime_factory=_factory,
        permissions_manager=permissions,
    )

    await manager.start()

    for thread_id, snapshot in expected.items():
        assert _permissions_path(tmp_path, thread_id).exists() is True
        assert await permissions.snapshot(thread_id) == snapshot
    await manager.aclose_all()


async def test_rename_and_archive_preserve_permissions_book(tmp_path: Path) -> None:
    """重命名和归档只改 metadata，thread id 本子保持原文件。"""
    thread_id = "thread-eeeeeeeeeeee"
    write_thread_metadata(tmp_path, _metadata(thread_id))
    permissions = PermissionsManager(tmp_path)
    await _seed_book(permissions, thread_id)
    manager = ThreadManager(
        _config(),
        kongming_home=tmp_path,
        runtime_factory=_factory,
        permissions_manager=permissions,
    )

    await manager.rename_thread(thread_id, "renamed")
    await manager.set_archived(thread_id, True)

    assert _permissions_path(tmp_path, thread_id).exists() is True
    assert (await permissions.snapshot(thread_id)).allow == (
        PermissionRuleRecord(expression="read_file", scope_cwd=None),
    )
