"""thread permissions 的异步模块门户。

``PermissionsManager`` 负责 per-thread 锁、内存 snapshot/编译缓存、DSL 匹配、
revision CAS 和记忆表达式生成。宿主、router 与决策引擎统一经过本门户访问本子。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from core.contracts import ApprovalRequest, Event, EventSink
from safety.approval._thread_permissions_store import ThreadPermissionsStore
from safety.approval.permissions_errors import (
    PermissionsDataError,
    PermissionsError,
    PermissionsExpressionError,
    PermissionsRevisionConflict,
    PermissionsStoreError,
)
from safety.approval.rule_models import (
    MatcherKind,
    PermissionEntry,
    PermissionResolution,
    PermissionRuleRecord,
    RememberRule,
    ThreadPermissionsSnapshot,
    Verdict,
)
from safety.approval.rule_parser import (
    canonical_cwd,
    matches_rule,
    parse_rule_expression,
    shell_prefix_tokens,
)

_SUBCOMMAND_TOOLS = frozenset({"git", "docker", "kubectl", "cargo"})
_SCRIPT_SUBCOMMAND_TOOLS = frozenset({"npm", "pnpm", "yarn", "uv"})
_UNREMEMBERABLE_SHELL_ROOTS = frozenset(
    {
        ".",
        "bash",
        "env",
        "eval",
        "exec",
        "fish",
        "node",
        "osascript",
        "perl",
        "php",
        "powershell",
        "pwsh",
        "python",
        "python3",
        "ruby",
        "sh",
        "source",
        "sudo",
        "xargs",
        "zsh",
    }
)


class _PermissionsStorage(Protocol):
    """Manager 消费的私有存储结构合同。"""

    async def read(self, thread_id: str) -> ThreadPermissionsSnapshot:
        """读取单 thread snapshot。"""
        ...

    async def write(
        self,
        thread_id: str,
        allow: tuple[PermissionRuleRecord, ...],
        deny: tuple[PermissionRuleRecord, ...],
        *,
        expected_revision: int,
    ) -> ThreadPermissionsSnapshot:
        """执行 revision CAS 写入。"""
        ...

    async def delete(self, thread_id: str) -> None:
        """删除单 thread 本子。"""
        ...


class PermissionsManager:
    """thread permissions 的唯一跨模块入口。"""

    def __init__(
        self,
        kongming_home: Path,
        *,
        _store: _PermissionsStorage | None = None,
        event_sinks: Sequence[EventSink] = (),
    ) -> None:
        """绑定 Kongming home、迁移事件出口，并允许测试注入存储 fake。"""
        root = kongming_home.expanduser().resolve(strict=False) / "safety" / "thread_permissions"
        self._store = _store or ThreadPermissionsStore(root)
        self._locks: dict[str, asyncio.Lock] = {}
        self._snapshots: dict[str, ThreadPermissionsSnapshot] = {}
        self._compiled: dict[str, tuple[int, tuple[PermissionEntry, ...]]] = {}
        self._event_sinks = tuple(event_sinks)
        self._emitted_migrations: set[tuple[str, int]] = set()

    async def resolve(
        self,
        thread_id: str,
        request: ApprovalRequest,
    ) -> PermissionResolution | None:
        """按 deny 再 allow 的固定优先级匹配当前 thread 本子。"""
        async with self._lock_for(thread_id):
            snapshot = await self._cached_or_load(thread_id)
            entries = self._compiled_entries(snapshot)
            for verdict in (Verdict.DENY, Verdict.ALLOW):
                for entry in entries:
                    if entry.verdict is verdict and _matches(entry, request):
                        return PermissionResolution(
                            verdict=entry.verdict,
                            expression=entry.expression,
                            scope_cwd=entry.scope_cwd,
                        )
            return None

    async def snapshot(self, thread_id: str) -> ThreadPermissionsSnapshot:
        """从磁盘刷新并返回当前 thread 的不可变 snapshot。"""
        async with self._lock_for(thread_id):
            snapshot = await self._store.read(thread_id)
            await self._emit_migration_once(snapshot)
            self._remember_snapshot(snapshot)
            return snapshot

    async def write_entry(
        self,
        thread_id: str,
        entry: PermissionEntry,
        *,
        expected_revision: int,
    ) -> ThreadPermissionsSnapshot:
        """把单条 allow/deny 写入目标 thread；同表达式从对侧列表移除。"""
        validated = _validate_entry(entry)
        async with self._lock_for(thread_id):
            current = await self._store.read(thread_id)
            await self._emit_migration_once(current)
            _require_revision(current, expected_revision)
            allow = list(current.allow)
            deny = list(current.deny)
            identity = _rule_identity(validated.rule)
            if validated.verdict is Verdict.ALLOW:
                deny = [item for item in deny if _rule_identity(item) != identity]
                if all(_rule_identity(item) != identity for item in allow):
                    allow.append(validated.rule)
            else:
                allow = [item for item in allow if _rule_identity(item) != identity]
                if all(_rule_identity(item) != identity for item in deny):
                    deny.append(validated.rule)
            if tuple(allow) == current.allow and tuple(deny) == current.deny:
                self._remember_snapshot(current)
                return current
            return await self._write_snapshot(
                current,
                allow=tuple(allow),
                deny=tuple(deny),
                expected_revision=expected_revision,
            )

    async def delete_entry(
        self,
        thread_id: str,
        entry: PermissionEntry,
        *,
        expected_revision: int,
    ) -> ThreadPermissionsSnapshot:
        """从指定 verdict 列表删除单条表达式；缺项视为幂等成功。"""
        validated = _validate_entry(entry)
        async with self._lock_for(thread_id):
            current = await self._store.read(thread_id)
            await self._emit_migration_once(current)
            _require_revision(current, expected_revision)
            allow = current.allow
            deny = current.deny
            identity = _rule_identity(validated.rule)
            if validated.verdict is Verdict.ALLOW:
                allow = tuple(item for item in allow if _rule_identity(item) != identity)
            else:
                deny = tuple(item for item in deny if _rule_identity(item) != identity)
            if allow == current.allow and deny == current.deny:
                self._remember_snapshot(current)
                return current
            return await self._write_snapshot(
                current,
                allow=allow,
                deny=deny,
                expected_revision=expected_revision,
            )

    async def replace(
        self,
        thread_id: str,
        *,
        allow: Sequence[PermissionRuleRecord],
        deny: Sequence[PermissionRuleRecord],
        expected_revision: int,
    ) -> ThreadPermissionsSnapshot:
        """整本替换 allow/deny，供 REST PUT 和迁移脚本复用。"""
        canonical_allow = _canonical_records(allow, Verdict.ALLOW)
        canonical_deny = _canonical_records(deny, Verdict.DENY)
        async with self._lock_for(thread_id):
            current = await self._store.read(thread_id)
            await self._emit_migration_once(current)
            _require_revision(current, expected_revision)
            if canonical_allow == current.allow and canonical_deny == current.deny:
                self._remember_snapshot(current)
                return current
            return await self._write_snapshot(
                current,
                allow=canonical_allow,
                deny=canonical_deny,
                expected_revision=expected_revision,
            )

    async def delete_thread(self, thread_id: str) -> None:
        """删除目标 thread 本子并清除对应内存缓存。"""
        async with self._lock_for(thread_id):
            await self._store.delete(thread_id)
            self._snapshots.pop(thread_id, None)
            self._compiled.pop(thread_id, None)

    def build_entry(
        self,
        expression: str,
        verdict: Verdict,
        *,
        scope_cwd: str | None = None,
    ) -> PermissionEntry:
        """解析 canonical DSL 并构造不可变 permission entry。"""
        try:
            matcher = parse_rule_expression(expression)
        except ValueError as exc:
            raise PermissionsExpressionError(
                f"invalid permission expression: {expression!r}"
            ) from exc
        if matcher.canonical_expression != expression:
            raise PermissionsExpressionError("permission expression must already be canonical")
        canonical_scope = _validate_scope(matcher.kind, verdict, scope_cwd)
        return PermissionEntry(
            rule=PermissionRuleRecord(
                expression=expression,
                scope_cwd=canonical_scope,
            ),
            verdict=verdict,
            matcher=matcher,
        )

    def build_remember_expression(self, request: ApprovalRequest) -> RememberRule | None:
        """从请求生成最小、安全且不携带 thread scope 的记忆候选。"""
        try:
            if request.tool_name == "run_shell":
                return _shell_remember_rule(request)
            if request.tool_name in {
                "read_file",
                "write_file",
                "edit_file",
                "list_dir",
                "view_file",
            }:
                return _path_remember_rule(request)
            parsed = parse_rule_expression(request.tool_name)
            return RememberRule(
                expression=parsed.canonical_expression,
                display_text=f"记住工具 {request.tool_name} 的选择",
                scope_cwd=None,
            )
        except (OSError, ValueError):
            return None

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        """按 thread 惰性创建独立 asyncio 锁。"""
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[thread_id] = lock
        return lock

    async def _cached_or_load(self, thread_id: str) -> ThreadPermissionsSnapshot:
        """命中内存快照时复用，首次访问时从磁盘加载。"""
        snapshot = self._snapshots.get(thread_id)
        if snapshot is None:
            snapshot = await self._store.read(thread_id)
            await self._emit_migration_once(snapshot)
            self._remember_snapshot(snapshot)
        return snapshot

    def _compiled_entries(self, snapshot: ThreadPermissionsSnapshot) -> tuple[PermissionEntry, ...]:
        """按 revision 缓存解析后的 deny/allow entry。"""
        cached = self._compiled.get(snapshot.thread_id)
        if cached is not None and cached[0] == snapshot.revision:
            return cached[1]
        entries = tuple(
            self.build_entry(
                record.expression,
                verdict,
                scope_cwd=record.scope_cwd,
            )
            for verdict, records in (
                (Verdict.DENY, snapshot.deny),
                (Verdict.ALLOW, snapshot.allow),
            )
            for record in records
        )
        self._compiled[snapshot.thread_id] = (snapshot.revision, entries)
        return entries

    async def _write_snapshot(
        self,
        current: ThreadPermissionsSnapshot,
        *,
        allow: tuple[PermissionRuleRecord, ...],
        deny: tuple[PermissionRuleRecord, ...],
        expected_revision: int,
    ) -> ThreadPermissionsSnapshot:
        """提交存储写入；冲突时刷新本地 cache 后继续抛出。"""
        try:
            updated = await self._store.write(
                current.thread_id,
                allow,
                deny,
                expected_revision=expected_revision,
            )
        except PermissionsRevisionConflict:
            latest = await self._store.read(current.thread_id)
            await self._emit_migration_once(latest)
            self._remember_snapshot(latest)
            raise
        self._remember_snapshot(updated)
        return updated

    async def _emit_migration_once(self, snapshot: ThreadPermissionsSnapshot) -> None:
        """把 Store 的一次性 v1→v2 结果 fan-out 为标准 EventSink 事件。"""
        summary = snapshot.migration_summary
        identity = (snapshot.thread_id, snapshot.revision)
        if summary is None or identity in self._emitted_migrations:
            return
        self._emitted_migrations.add(identity)
        event = Event(
            kind="permissions.migrated.v2",
            run_id=f"permissions-migration:{snapshot.thread_id}",
            payload={
                "thread_id": snapshot.thread_id,
                "from_schema_version": summary.from_schema_version,
                "to_schema_version": summary.to_schema_version,
                "invalidated_shell_allow_count": summary.invalidated_shell_allow_count,
                "backup_path": summary.backup_path,
            },
        )
        await asyncio.gather(
            *(sink.emit(event) for sink in self._event_sinks),
            return_exceptions=True,
        )

    def _remember_snapshot(self, snapshot: ThreadPermissionsSnapshot) -> None:
        """更新内存真值并淘汰旧 revision 的编译缓存。"""
        previous = self._snapshots.get(snapshot.thread_id)
        self._snapshots[snapshot.thread_id] = snapshot
        if previous is None or previous.revision != snapshot.revision:
            self._compiled.pop(snapshot.thread_id, None)


def _validate_entry(entry: PermissionEntry) -> PermissionEntry:
    """重解析 entry，阻止伪造 matcher 或非 canonical expression。"""
    try:
        parsed = parse_rule_expression(entry.expression)
    except ValueError as exc:
        raise PermissionsExpressionError(
            f"invalid permission expression: {entry.expression!r}"
        ) from exc
    expected_scope = _validate_scope(
        parsed.kind,
        entry.verdict,
        entry.scope_cwd,
    )
    if (
        parsed.canonical_expression != entry.expression
        or parsed != entry.matcher
        or expected_scope != entry.scope_cwd
    ):
        raise PermissionsExpressionError(
            "permission entry matcher differs from canonical expression"
        )
    return entry


def _canonical_records(
    records: Sequence[PermissionRuleRecord],
    verdict: Verdict,
) -> tuple[PermissionRuleRecord, ...]:
    """解析结构化规则，校验 scope，并按完整身份去重。"""
    result: list[PermissionRuleRecord] = []
    seen: set[tuple[str, str | None]] = set()
    for record in records:
        try:
            parsed = parse_rule_expression(record.expression)
            if parsed.canonical_expression != record.expression:
                raise ValueError("permission expression must already be canonical")
            scope_cwd = _validate_scope(parsed.kind, verdict, record.scope_cwd)
        except PermissionsExpressionError:
            raise
        except ValueError as exc:
            raise PermissionsExpressionError(
                f"invalid permission expression: {record.expression!r}"
            ) from exc
        validated = PermissionRuleRecord(
            expression=record.expression,
            scope_cwd=scope_cwd,
        )
        identity = _rule_identity(validated)
        if identity not in seen:
            result.append(validated)
            seen.add(identity)
    return tuple(result)


def _require_revision(snapshot: ThreadPermissionsSnapshot, expected_revision: int) -> None:
    """在进入写路径前执行快速 revision 校验。"""
    if expected_revision < 0:
        raise ValueError("expected_revision must be non-negative")
    if snapshot.revision != expected_revision:
        raise PermissionsRevisionConflict(
            thread_id=snapshot.thread_id,
            expected_revision=expected_revision,
            actual_revision=snapshot.revision,
        )


def _request_base_cwd(request: ApprovalRequest) -> str:
    """返回 path matcher 的 runtime 基准 cwd。"""
    raw = request.metadata.get("cwd")
    if isinstance(raw, str) and raw.strip():
        return canonical_cwd(raw)
    return canonical_cwd(str(Path.cwd()))


def _matches(entry: PermissionEntry, request: ApprovalRequest) -> bool:
    """匹配单条 entry；Shell scope 只读取 prepared execution scope。"""
    try:
        if entry.matcher.kind is MatcherKind.SHELL_PREFIX:
            if not matches_rule(entry.matcher, request, cwd="/"):
                return False
            request_cwd = request.execution_scope.cwd
            if not isinstance(request_cwd, str):
                return False
            canonical_request_cwd = canonical_cwd(request_cwd)
            if entry.scope_cwd is None:
                return entry.verdict is Verdict.DENY
            return canonical_request_cwd == entry.scope_cwd
        return matches_rule(entry.matcher, request, cwd=_request_base_cwd(request))
    except (OSError, ValueError):
        return False


def _validate_scope(
    matcher_kind: MatcherKind,
    verdict: Verdict,
    scope_cwd: str | None,
) -> str | None:
    """收敛规则 scope；Shell allow 必须 exact cwd，非 Shell 必须为空。"""
    if matcher_kind is MatcherKind.SHELL_PREFIX:
        if scope_cwd is None:
            if verdict is Verdict.DENY:
                return None
            raise PermissionsExpressionError("shell allow rule requires scope_cwd")
        try:
            canonical = canonical_cwd(scope_cwd)
        except ValueError as exc:
            raise PermissionsExpressionError("scope_cwd must be canonical absolute path") from exc
        if canonical != scope_cwd:
            raise PermissionsExpressionError("scope_cwd must already be canonical")
        return canonical
    if scope_cwd is not None:
        raise PermissionsExpressionError("non-shell rule must use scope_cwd=null")
    return None


def _rule_identity(record: PermissionRuleRecord) -> tuple[str, str | None]:
    """返回结构化规则的去重和对侧移除身份。"""
    return record.expression, record.scope_cwd


def _shell_remember_rule(request: ApprovalRequest) -> RememberRule | None:
    """为安全的 shell token 前缀生成记忆候选。"""
    command = request.arguments.get("command")
    if not isinstance(command, str):
        return None
    tokens = shell_prefix_tokens(command)
    if not tokens or tokens[0] in _UNREMEMBERABLE_SHELL_ROOTS:
        return None
    if tokens[0] in _SCRIPT_SUBCOMMAND_TOOLS:
        if len(tokens) < 3 or tokens[1] != "run":
            return None
        count = 3
    else:
        count = 2 if tokens[0] in _SUBCOMMAND_TOOLS and len(tokens) > 1 else 1
    prefix = " ".join(tokens[:count])
    parsed = parse_rule_expression(f"run_shell({prefix}:*)")
    raw_scope = request.execution_scope.cwd
    if not isinstance(raw_scope, str):
        return None
    scope_cwd = canonical_cwd(raw_scope)
    return RememberRule(
        expression=parsed.canonical_expression,
        display_text=f"记住目录 {scope_cwd} 中以 {prefix} 开头命令的选择",
        scope_cwd=scope_cwd,
    )


def _path_remember_rule(request: ApprovalRequest) -> RememberRule | None:
    """为请求中的绝对或 cwd 相对路径生成记忆候选。"""
    raw_path = request.arguments.get("path", request.arguments.get("file_path"))
    raw_cwd = request.metadata.get("cwd")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path(canonical_cwd(raw_cwd)) / path
    canonical_path = path.resolve(strict=False).as_posix()
    parsed = parse_rule_expression(f"{request.tool_name}({canonical_path}/**)")
    return RememberRule(
        expression=parsed.canonical_expression,
        display_text=f"记住 {request.tool_name} 访问 {canonical_path} 的选择",
        scope_cwd=None,
    )


__all__ = [
    "PermissionsDataError",
    "PermissionsError",
    "PermissionsExpressionError",
    "PermissionsManager",
    "PermissionsRevisionConflict",
    "PermissionsStoreError",
]
