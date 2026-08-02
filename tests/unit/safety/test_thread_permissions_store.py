"""验证 thread permissions 私有文件存储的安全与原子性合同。

覆盖 SHA-256 路径、严格 JSON schema、文件内 thread 身份、revision CAS、
原子替换失败回滚和幂等删除。测试只通过公开异步方法触发真实文件 I/O。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from safety.approval._thread_permissions_store import ThreadPermissionsStore
from safety.approval.permissions_errors import (
    PermissionsDataError,
    PermissionsRevisionConflict,
    PermissionsStoreError,
)
from safety.approval.rule_models import PermissionRuleRecord


def _rule(expression: str, scope_cwd: Path | str | None = None) -> PermissionRuleRecord:
    """构造测试用 schema v2 规则。"""
    scope = Path(scope_cwd).resolve().as_posix() if scope_cwd is not None else None
    return PermissionRuleRecord(expression=expression, scope_cwd=scope)


@pytest.mark.asyncio
async def test_missing_file_returns_empty_snapshot_without_materializing(tmp_path: Path) -> None:
    """首次读取在文件锁下返回空快照，JSON 延迟到首次写入。"""
    root = tmp_path / "safety" / "thread_permissions"
    store = ThreadPermissionsStore(root)

    snapshot = await store.read("thread-a")

    assert snapshot.thread_id == "thread-a"
    assert snapshot.revision == 0
    assert snapshot.allow == ()
    assert snapshot.deny == ()
    assert snapshot.updated_at is None
    assert snapshot.schema_version == 2
    assert not store.path_for("thread-a").exists()


@pytest.mark.asyncio
async def test_hash_path_keeps_path_like_thread_id_inside_root(tmp_path: Path) -> None:
    """路径形态的 thread id 只参与 hash，无法穿越存储根目录。"""
    root = tmp_path / "permissions"
    store = ThreadPermissionsStore(root)
    thread_id = "../../outside/thread-a"

    written = await store.write(
        thread_id,
        (_rule("read_file"),),
        (),
        expected_revision=0,
    )
    expected_name = hashlib.sha256(thread_id.encode("utf-8")).hexdigest() + ".json"
    path = store.path_for(thread_id)

    assert written.revision == 1
    assert path.parent == root.resolve(strict=False)
    assert path.name == expected_name
    assert path.is_file()
    assert not (tmp_path / "outside").exists()


@pytest.mark.asyncio
async def test_read_rejects_file_identity_mismatch(tmp_path: Path) -> None:
    """hash 文件内的真实 thread_id 必须和读取目标完全一致。"""
    store = ThreadPermissionsStore(tmp_path / "permissions")
    await store.write("thread-a", (), (), expected_revision=0)
    path = store.path_for("thread-a")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["thread_id"] = "thread-b"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PermissionsDataError, match="identity mismatch"):
        await store.read("thread-a")


@pytest.mark.asyncio
async def test_read_rejects_unknown_json_field(tmp_path: Path) -> None:
    """磁盘 JSON 未知字段触发严格 schema 错误。"""
    store = ThreadPermissionsStore(tmp_path / "permissions")
    await store.write("thread-a", (), (), expected_revision=0)
    path = store.path_for("thread-a")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PermissionsDataError, match="invalid permissions file"):
        await store.read("thread-a")


@pytest.mark.asyncio
async def test_write_enforces_revision_cas(tmp_path: Path) -> None:
    """过期 revision 写入失败，并保留磁盘上的最新快照。"""
    store = ThreadPermissionsStore(tmp_path / "permissions")
    first = await store.write("thread-a", (_rule("read_file"),), (), expected_revision=0)

    with pytest.raises(PermissionsRevisionConflict) as caught:
        await store.write("thread-a", (_rule("list_dir"),), (), expected_revision=0)

    latest = await store.read("thread-a")
    assert caught.value.expected_revision == 0
    assert caught.value.actual_revision == 1
    assert first == latest


@pytest.mark.asyncio
async def test_concurrent_writers_from_same_revision_have_one_winner(tmp_path: Path) -> None:
    """两个跨线程写入共享旧 revision 时，文件锁与 CAS 只允许一个提交。"""
    store = ThreadPermissionsStore(tmp_path / "permissions")

    async def _write(expression: str) -> str:
        try:
            await store.write(
                "thread-a",
                (_rule(expression),),
                (),
                expected_revision=0,
            )
        except PermissionsRevisionConflict:
            return "conflict"
        return "written"

    results = await asyncio.gather(_write("read_file"), _write("list_dir"))

    assert sorted(results) == ["conflict", "written"]
    assert (await store.read("thread-a")).revision == 1


@pytest.mark.asyncio
async def test_atomic_replace_failure_preserves_previous_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原子替换失败时旧 JSON 保持完整，临时文件被清理。"""
    store = ThreadPermissionsStore(tmp_path / "permissions")
    first = await store.write("thread-a", (_rule("read_file"),), (), expected_revision=0)

    def fail_replace(_source: Path, _target: Path) -> None:
        """模拟操作系统拒绝原子替换。"""
        raise OSError("replace failed")

    monkeypatch.setattr("safety.approval._thread_permissions_store.os.replace", fail_replace)

    with pytest.raises(PermissionsStoreError, match="replace failed"):
        await store.write("thread-a", (_rule("list_dir"),), (), expected_revision=1)

    assert await store.read("thread-a") == first
    assert list((tmp_path / "permissions").glob("*.tmp.*")) == []


@pytest.mark.asyncio
async def test_delete_is_idempotent_and_removes_json(tmp_path: Path) -> None:
    """重复删除均成功，目标 JSON 消失并回到空快照。"""
    store = ThreadPermissionsStore(tmp_path / "permissions")
    await store.write("thread-a", (_rule("read_file"),), (), expected_revision=0)

    await store.delete("thread-a")
    await store.delete("thread-a")

    assert not store.path_for("thread-a").exists()
    assert (await store.read("thread-a")).revision == 0


@pytest.mark.asyncio
async def test_v1_migration_backs_up_and_invalidates_unscoped_shell_allow(
    tmp_path: Path,
) -> None:
    """v1 混合规则安全迁移：保留非 Shell allow 与全部 deny。"""
    store = ThreadPermissionsStore(tmp_path / "permissions")
    path = store.path_for("thread-a")
    path.parent.mkdir(parents=True)
    original = {
        "schema_version": 1,
        "thread_id": "thread-a",
        "revision": 4,
        "allow": ["read_file", "run_shell(git status:*)"],
        "deny": ["run_shell(curl:*)", "write_file"],
        "updated_at": "2026-07-24T10:00:00Z",
    }
    path.write_text(json.dumps(original), encoding="utf-8")

    snapshot = await store.read("thread-a")
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert snapshot.schema_version == 2
    assert snapshot.revision == 5
    assert snapshot.allow == (_rule("read_file"),)
    assert snapshot.deny == (_rule("run_shell(curl:*)"), _rule("write_file"))
    assert snapshot.migration_summary is not None
    assert snapshot.migration_summary.invalidated_shell_allow_count == 1
    assert json.loads(store.backup_path_for("thread-a").read_text(encoding="utf-8")) == original
    assert persisted["schema_version"] == 2
    assert persisted["allow"] == [{"expression": "read_file", "scope_cwd": None}]
    restarted = await ThreadPermissionsStore(tmp_path / "permissions").read("thread-a")
    assert restarted.allow == snapshot.allow
    assert restarted.deny == snapshot.deny
    assert restarted.revision == snapshot.revision
    assert restarted.migration_summary is None


@pytest.mark.asyncio
async def test_v1_migration_replace_failure_preserves_original_and_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """迁移原子替换失败时 v1 原文件与备份都可恢复。"""
    store = ThreadPermissionsStore(tmp_path / "permissions")
    path = store.path_for("thread-a")
    path.parent.mkdir(parents=True)
    original = {
        "schema_version": 1,
        "thread_id": "thread-a",
        "revision": 1,
        "allow": ["run_shell(git:*)"],
        "deny": [],
        "updated_at": "2026-07-24T10:00:00Z",
    }
    original_text = json.dumps(original)
    path.write_text(original_text, encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        """模拟迁移原子替换失败。"""
        raise OSError("migration replace failed")

    monkeypatch.setattr("safety.approval._thread_permissions_store.os.replace", fail_replace)

    with pytest.raises(PermissionsStoreError, match="migration replace failed"):
        await store.read("thread-a")

    assert path.read_text(encoding="utf-8") == original_text
    assert store.backup_path_for("thread-a").read_text(encoding="utf-8") == original_text


@pytest.mark.asyncio
async def test_v2_rollback_drops_scoped_shell_allow_and_keeps_deny(tmp_path: Path) -> None:
    """回滚到 v1 时输出明确损失清单并保持保守 deny。"""
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    store = ThreadPermissionsStore(tmp_path / "permissions")
    await store.write(
        "thread-a",
        (_rule("read_file"), _rule("run_shell(git:*)", cwd)),
        (_rule("run_shell(curl:*)"),),
        expected_revision=0,
    )

    lost = await store.rollback_to_v1("thread-a")
    payload = json.loads(store.path_for("thread-a").read_text(encoding="utf-8"))

    assert lost == ("run_shell(git:*)",)
    assert payload["schema_version"] == 1
    assert payload["allow"] == ["read_file"]
    assert payload["deny"] == ["run_shell(curl:*)"]


@pytest.mark.parametrize("thread_id", ["", " thread-a", "thread-a ", "thread\n-a"])
@pytest.mark.asyncio
async def test_thread_id_rejects_unstable_text(tmp_path: Path, thread_id: str) -> None:
    """空白和控制字符 thread id 在触碰文件系统前被拒绝。"""
    store = ThreadPermissionsStore(tmp_path / "permissions")

    with pytest.raises(ValueError):
        await store.read(thread_id)

    assert not (tmp_path / "permissions").exists()
