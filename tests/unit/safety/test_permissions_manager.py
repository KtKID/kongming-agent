"""验证 PermissionsManager 的 thread 隔离、匹配与并发状态机。

测试覆盖 deny 优先、跨进程恢复、write/delete/replace revision CAS、缓存回滚、
同 thread 串行和不同 thread 并行，所有持久化用例走真实 JSON store。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.contracts import ApprovalRequest, Event, ToolExecutionScope
from safety.approval._thread_permissions_store import ThreadPermissionsStore
from safety.approval.permissions_errors import (
    PermissionsDataError,
    PermissionsRevisionConflict,
)
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.rule_models import PermissionRuleRecord, ThreadPermissionsSnapshot, Verdict


def _rule(expression: str, scope_cwd: Path | str | None = None) -> PermissionRuleRecord:
    """构造测试用结构化规则。"""
    scope = Path(scope_cwd).resolve().as_posix() if scope_cwd is not None else None
    return PermissionRuleRecord(expression=expression, scope_cwd=scope)


class _EventSink:
    """记录 PermissionsManager 发出的迁移事件。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        """保存单条事件。"""
        self.events.append(event)


def _request(
    tool_name: str,
    *,
    arguments: dict[str, object] | None = None,
    cwd: Path | None = None,
) -> ApprovalRequest:
    """构造带稳定运行坐标和 cwd 的审批请求。"""
    return ApprovalRequest(
        run_id="run-1",
        session_id="session-1",
        turn=1,
        call_id="call-1",
        tool_name=tool_name,
        arguments={} if arguments is None else arguments,
        execution_scope=ToolExecutionScope(
            cwd=(cwd or Path.cwd()).resolve().as_posix() if tool_name == "run_shell" else None
        ),
        metadata={"cwd": str(cwd or Path.cwd())},
    )


@pytest.mark.asyncio
async def test_deny_wins_allow_and_other_thread_stays_empty(tmp_path: Path) -> None:
    """同表达式双边命中时 deny 优先，thread B 不继承 A 的本子。"""
    manager = PermissionsManager(tmp_path)
    await manager.replace(
        "thread-a",
        allow=[_rule("read_file")],
        deny=[_rule("read_file")],
        expected_revision=0,
    )

    resolved_a = await manager.resolve("thread-a", _request("read_file"))
    resolved_b = await manager.resolve("thread-b", _request("read_file"))

    assert resolved_a is not None
    assert resolved_a.verdict is Verdict.DENY
    assert resolved_a.expression == "read_file"
    assert resolved_b is None
    assert (await manager.snapshot("thread-b")).revision == 0


@pytest.mark.asyncio
async def test_persisted_snapshot_survives_manager_restart(tmp_path: Path) -> None:
    """新 Manager 进程视角从磁盘恢复同一 thread 的规则。"""
    first = PermissionsManager(tmp_path)
    await first.replace(
        "thread-a",
        allow=[_rule("read_file"), _rule("list_dir")],
        deny=[],
        expected_revision=0,
    )

    restarted = PermissionsManager(tmp_path)
    snapshot = await restarted.snapshot("thread-a")
    resolution = await restarted.resolve("thread-a", _request("list_dir"))

    assert snapshot.revision == 1
    assert snapshot.allow == (_rule("read_file"), _rule("list_dir"))
    assert resolution is not None
    assert resolution.verdict is Verdict.ALLOW


@pytest.mark.asyncio
async def test_write_entry_moves_expression_between_verdicts_and_delete_is_idempotent(
    tmp_path: Path,
) -> None:
    """记忆写回维持单边归属，删除缺项保持当前 revision。"""
    manager = PermissionsManager(tmp_path)
    allow_entry = manager.build_entry("read_file", Verdict.ALLOW)
    deny_entry = manager.build_entry("read_file", Verdict.DENY)

    allowed = await manager.write_entry(
        "thread-a",
        allow_entry,
        expected_revision=0,
    )
    repeated_allow = await manager.write_entry(
        "thread-a",
        allow_entry,
        expected_revision=allowed.revision,
    )
    denied = await manager.write_entry(
        "thread-a",
        deny_entry,
        expected_revision=repeated_allow.revision,
    )
    deleted = await manager.delete_entry(
        "thread-a",
        deny_entry,
        expected_revision=denied.revision,
    )
    unchanged = await manager.delete_entry(
        "thread-a",
        deny_entry,
        expected_revision=deleted.revision,
    )

    assert allowed.allow == (_rule("read_file"),)
    assert repeated_allow == allowed
    assert denied.allow == ()
    assert denied.deny == (_rule("read_file"),)
    assert deleted.revision == 3
    assert deleted.deny == ()
    assert unchanged == deleted


@pytest.mark.asyncio
async def test_stale_revision_preserves_disk_and_refreshes_cache(tmp_path: Path) -> None:
    """冲突后磁盘与 Manager cache 都保持最新 revision 和内容。"""
    manager = PermissionsManager(tmp_path)
    latest = await manager.replace(
        "thread-a",
        allow=[_rule("read_file")],
        deny=[],
        expected_revision=0,
    )

    with pytest.raises(PermissionsRevisionConflict) as caught:
        await manager.replace(
            "thread-a",
            allow=[_rule("list_dir")],
            deny=[],
            expected_revision=0,
        )

    snapshot = await manager.snapshot("thread-a")
    resolution = await manager.resolve("thread-a", _request("read_file"))
    assert caught.value.actual_revision == latest.revision
    assert snapshot == latest
    assert resolution is not None
    assert resolution.verdict is Verdict.ALLOW


@pytest.mark.asyncio
async def test_replace_deduplicates_and_rejects_noncanonical_expression(tmp_path: Path) -> None:
    """整本替换稳定去重，并拒绝需要 canonicalize 的输入。"""
    manager = PermissionsManager(tmp_path)
    snapshot = await manager.replace(
        "thread-a",
        allow=[_rule("read_file"), _rule("read_file")],
        deny=[],
        expected_revision=0,
    )

    assert snapshot.allow == (_rule("read_file"),)
    with pytest.raises((PermissionsDataError, ValueError)):
        await manager.replace(
            "thread-a",
            allow=[_rule(" run_shell(git:*)")],
            deny=[],
            expected_revision=snapshot.revision,
        )


@pytest.mark.asyncio
async def test_delete_thread_clears_file_and_cache(tmp_path: Path) -> None:
    """删除 thread 后旧 snapshot 不再参与 resolve。"""
    manager = PermissionsManager(tmp_path)
    await manager.replace(
        "thread-a",
        allow=[_rule("read_file")],
        deny=[],
        expected_revision=0,
    )
    assert await manager.resolve("thread-a", _request("read_file")) is not None

    await manager.delete_thread("thread-a")

    assert await manager.resolve("thread-a", _request("read_file")) is None
    assert (await manager.snapshot("thread-a")).revision == 0


def test_build_remember_expression_uses_canonical_shell_and_path_dsl(tmp_path: Path) -> None:
    """记忆候选生成稳定 shell 前缀和绝对路径表达式。"""
    manager = PermissionsManager(tmp_path)

    shell = manager.build_remember_expression(
        _request("run_shell", arguments={"command": "git status --short"}, cwd=tmp_path)
    )
    path = manager.build_remember_expression(
        _request("write_file", arguments={"path": "notes/a.md"}, cwd=tmp_path)
    )

    assert shell is not None
    assert shell.expression == "run_shell(git status:*)"
    assert shell.scope_cwd == tmp_path.resolve().as_posix()
    assert path is not None
    assert path.expression == f"write_file({(tmp_path / 'notes' / 'a.md').as_posix()}/**)"


@pytest.mark.asyncio
async def test_shell_allow_matches_command_and_exact_effective_cwd(tmp_path: Path) -> None:
    """同命令只在审批时冻结的 exact cwd 静默命中。"""
    cwd_a = tmp_path / "a"
    cwd_b = tmp_path / "b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    manager = PermissionsManager(tmp_path)
    await manager.replace(
        "thread-a",
        allow=[_rule("run_shell(git status:*)", cwd_a)],
        deny=[],
        expected_revision=0,
    )

    same = await manager.resolve(
        "thread-a",
        _request("run_shell", arguments={"command": "git status --short"}, cwd=cwd_a),
    )
    crossed = await manager.resolve(
        "thread-a",
        _request("run_shell", arguments={"command": "git status --short"}, cwd=cwd_b),
    )
    other_command = await manager.resolve(
        "thread-a",
        _request("run_shell", arguments={"command": "git diff"}, cwd=cwd_a),
    )

    assert same is not None
    assert same.verdict is Verdict.ALLOW
    assert same.scope_cwd == cwd_a.resolve().as_posix()
    assert crossed is None
    assert other_command is None


@pytest.mark.asyncio
async def test_shell_global_deny_matches_all_cwds_and_unscoped_allow_is_rejected(
    tmp_path: Path,
) -> None:
    """Shell deny 可覆盖全 cwd，Shell allow 强制携带 scope。"""
    cwd_a = tmp_path / "a"
    cwd_b = tmp_path / "b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    manager = PermissionsManager(tmp_path)
    await manager.replace(
        "thread-a",
        allow=[],
        deny=[_rule("run_shell(curl:*)")],
        expected_revision=0,
    )

    for cwd in (cwd_a, cwd_b):
        resolution = await manager.resolve(
            "thread-a",
            _request("run_shell", arguments={"command": "curl example.com"}, cwd=cwd),
        )
        assert resolution is not None
        assert resolution.verdict is Verdict.DENY

    with pytest.raises(PermissionsDataError, match="scope_cwd"):
        await manager.replace(
            "thread-a",
            allow=[_rule("run_shell(git:*)")],
            deny=[],
            expected_revision=1,
        )


@pytest.mark.asyncio
async def test_shell_rule_identity_includes_scope_for_write_delete_and_dedupe(
    tmp_path: Path,
) -> None:
    """同表达式的两个 cwd 规则独立保存、移动和删除。"""
    cwd_a = tmp_path / "a"
    cwd_b = tmp_path / "b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    manager = PermissionsManager(tmp_path)
    expression = "run_shell(git status:*)"
    initial = await manager.replace(
        "thread-a",
        allow=[
            _rule(expression, cwd_a),
            _rule(expression, cwd_b),
            _rule(expression, cwd_a),
        ],
        deny=[],
        expected_revision=0,
    )
    assert initial.allow == (
        _rule(expression, cwd_a),
        _rule(expression, cwd_b),
    )

    denied_a = await manager.write_entry(
        "thread-a",
        manager.build_entry(
            expression,
            Verdict.DENY,
            scope_cwd=cwd_a.resolve().as_posix(),
        ),
        expected_revision=1,
    )
    assert denied_a.allow == (_rule(expression, cwd_b),)
    assert denied_a.deny == (_rule(expression, cwd_a),)

    deleted_a = await manager.delete_entry(
        "thread-a",
        manager.build_entry(
            expression,
            Verdict.DENY,
            scope_cwd=cwd_a.resolve().as_posix(),
        ),
        expected_revision=2,
    )
    assert deleted_a.allow == (_rule(expression, cwd_b),)
    assert deleted_a.deny == ()


@pytest.mark.asyncio
async def test_manager_emits_v1_migration_event_once(tmp_path: Path) -> None:
    """Store 返回迁移 summary 后，Manager 通过 EventSink 只广播一次。"""
    root = tmp_path / "safety" / "thread_permissions"
    store = ThreadPermissionsStore(root)
    path = store.path_for("thread-a")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "thread_id": "thread-a",
                "revision": 0,
                "allow": ["run_shell(git:*)"],
                "deny": [],
                "updated_at": None,
            }
        ),
        encoding="utf-8",
    )
    sink = _EventSink()
    manager = PermissionsManager(tmp_path, _store=store, event_sinks=[sink])

    first = await manager.snapshot("thread-a")
    second = await manager.snapshot("thread-a")

    assert first.migration_summary is not None
    assert second.migration_summary is None
    assert [event.kind for event in sink.events] == ["permissions.migrated.v2"]
    assert sink.events[0].payload["invalidated_shell_allow_count"] == 1


class _BlockingReadStore:
    """用于观察 Manager per-thread 锁粒度的结构化存储 fake。"""

    def __init__(self) -> None:
        """初始化阻塞开关和每个 thread 的读取次数。"""
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.read_counts: dict[str, int] = {}

    async def read(self, thread_id: str) -> ThreadPermissionsSnapshot:
        """thread-a 首次读取阻塞，其它 thread 立即返回。"""
        self.read_counts[thread_id] = self.read_counts.get(thread_id, 0) + 1
        if thread_id == "thread-a" and self.read_counts[thread_id] == 1:
            self.started.set()
            await self.release.wait()
        return ThreadPermissionsSnapshot(
            thread_id=thread_id,
            revision=0,
            allow=(),
            deny=(),
            updated_at=None,
        )

    async def write(
        self,
        thread_id: str,
        allow: tuple[PermissionRuleRecord, ...],
        deny: tuple[PermissionRuleRecord, ...],
        *,
        expected_revision: int,
    ) -> ThreadPermissionsSnapshot:
        """并发锁测试无需写入，调用即暴露测试错误。"""
        raise AssertionError(f"unexpected write: {thread_id}, {allow}, {deny}, {expected_revision}")

    async def delete(self, thread_id: str) -> None:
        """并发锁测试无需删除，调用即暴露测试错误。"""
        raise AssertionError(f"unexpected delete: {thread_id}")


@pytest.mark.asyncio
async def test_different_threads_do_not_share_async_lock(tmp_path: Path) -> None:
    """thread-a I/O 阻塞期间 thread-b snapshot 仍可完成。"""
    store = _BlockingReadStore()
    manager = PermissionsManager(tmp_path, _store=store)
    blocked = asyncio.create_task(manager.snapshot("thread-a"))
    await store.started.wait()

    other = await asyncio.wait_for(manager.snapshot("thread-b"), timeout=0.2)
    store.release.set()
    await blocked

    assert other.thread_id == "thread-b"
    assert store.read_counts == {"thread-a": 1, "thread-b": 1}


@pytest.mark.asyncio
async def test_same_thread_operations_are_serialized(tmp_path: Path) -> None:
    """同一 thread 的第二次读取会等待第一次释放 per-thread 锁。"""
    store = _BlockingReadStore()
    manager = PermissionsManager(tmp_path, _store=store)
    first = asyncio.create_task(manager.snapshot("thread-a"))
    await store.started.wait()
    second = asyncio.create_task(manager.snapshot("thread-a"))
    await asyncio.sleep(0)

    assert store.read_counts == {"thread-a": 1}
    store.release.set()
    await asyncio.gather(first, second)
    assert store.read_counts == {"thread-a": 2}
