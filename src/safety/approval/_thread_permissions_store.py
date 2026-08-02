"""thread permissions 的私有 JSON 文件存储。

本模块把每个 thread 映射到 SHA-256 文件名，严格校验 schema v2，并在独占
文件锁内执行 revision CAS、v1 安全迁移、备份、临时文件 fsync 和原子替换。
所有同步文件 I/O 都由公开异步方法通过 ``asyncio.to_thread`` 调用。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from safety.approval.permissions_errors import (
    PermissionsDataError,
    PermissionsRevisionConflict,
    PermissionsStoreError,
)
from safety.approval.rule_models import (
    MatcherKind,
    PermissionRuleRecord,
    PermissionsMigrationSummary,
    ThreadPermissionsSnapshot,
    Verdict,
)
from safety.approval.rule_parser import parse_rule_expression

_SCHEMA_VERSION: Literal[2] = 2
_IS_WINDOWS = sys.platform.startswith("win")
_WINDOWS_LOCK_MODE = 1
_WINDOWS_UNLOCK_MODE = 0
_MAX_THREAD_ID_CHARS = 512


class _StoredPermissionRule(BaseModel):
    """磁盘上的单条结构化 permission 规则。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    expression: str
    scope_cwd: str | None

    @field_validator("expression")
    @classmethod
    def _validate_expression(cls, value: str) -> str:
        """要求表达式为 canonical DSL。"""
        if not value or value != value.strip():
            raise ValueError("permission expression must be non-empty canonical text")
        parsed = parse_rule_expression(value)
        if parsed.canonical_expression != value:
            raise ValueError("permission expression must already be canonical")
        return value

    @field_validator("scope_cwd")
    @classmethod
    def _validate_scope_cwd(cls, value: str | None) -> str | None:
        """要求可选 scope 为 canonical absolute path。"""
        if value is None:
            return None
        return _canonical_scope_cwd(value)


class _StoredPermissionsV1(BaseModel):
    """仅用于首次读取迁移的旧 schema v1。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    thread_id: str
    revision: Annotated[int, Field(ge=0)]
    allow: list[str]
    deny: list[str]
    updated_at: str | None

    @field_validator("thread_id")
    @classmethod
    def _validate_stored_thread_id(cls, value: str) -> str:
        """复用稳定 thread id 约束校验磁盘身份。"""
        return _validate_thread_id(value)

    @field_validator("allow", "deny")
    @classmethod
    def _validate_expression_list(cls, values: list[str]) -> list[str]:
        """旧表达式数组仍要求 canonical 且不重复。"""
        if len(values) != len(set(values)):
            raise ValueError("permission expressions must not contain duplicates")
        for value in values:
            if not value or value != value.strip():
                raise ValueError("permission expressions must be non-empty canonical text")
            parsed = parse_rule_expression(value)
            if parsed.canonical_expression != value:
                raise ValueError("permission expression must already be canonical")
        return values

    @field_validator("updated_at")
    @classmethod
    def _validate_updated_at(cls, value: str | None) -> str | None:
        """updated_at 必须是带时区的 ISO-8601 文本。"""
        return _validate_updated_at(value)


class _StoredPermissions(BaseModel):
    """磁盘 JSON 的严格 schema v2。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2]
    thread_id: str
    revision: Annotated[int, Field(ge=0)]
    allow: list[_StoredPermissionRule]
    deny: list[_StoredPermissionRule]
    updated_at: str | None

    @field_validator("thread_id")
    @classmethod
    def _validate_stored_thread_id(cls, value: str) -> str:
        """复用稳定 thread id 约束校验磁盘身份。"""
        return _validate_thread_id(value)

    @field_validator("updated_at")
    @classmethod
    def _validate_updated_at(cls, value: str | None) -> str | None:
        """updated_at 必须是带时区的 ISO-8601 文本。"""
        return _validate_updated_at(value)

    @model_validator(mode="after")
    def _validate_rule_sets(self) -> _StoredPermissions:
        """校验规则身份唯一性和 verdict 对 scope 的约束。"""
        for verdict, rules in ((Verdict.ALLOW, self.allow), (Verdict.DENY, self.deny)):
            identities = [(item.expression, item.scope_cwd) for item in rules]
            if len(identities) != len(set(identities)):
                raise ValueError("permission rules must not contain duplicate identities")
            for item in rules:
                _validate_rule_scope(
                    PermissionRuleRecord(
                        expression=item.expression,
                        scope_cwd=item.scope_cwd,
                    ),
                    verdict,
                )
        return self


class ThreadPermissionsStore:
    """按 thread 分文件保存 permissions snapshot 的私有 helper。"""

    def __init__(self, root: Path) -> None:
        """绑定 ``<kongming_home>/safety/thread_permissions`` 根目录。"""
        self._root = root.expanduser().resolve(strict=False)

    async def read(self, thread_id: str) -> ThreadPermissionsSnapshot:
        """在独占文件锁内读取并按需迁移目标 thread snapshot。"""
        validated = _validate_thread_id(thread_id)
        return await asyncio.to_thread(self._read_sync, validated)

    async def write(
        self,
        thread_id: str,
        allow: tuple[PermissionRuleRecord, ...],
        deny: tuple[PermissionRuleRecord, ...],
        *,
        expected_revision: int,
    ) -> ThreadPermissionsSnapshot:
        """在独占文件锁内完成 revision CAS 和 schema v2 原子写入。"""
        validated = _validate_thread_id(thread_id)
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        return await asyncio.to_thread(
            self._write_sync,
            validated,
            allow,
            deny,
            expected_revision,
        )

    async def delete(self, thread_id: str) -> None:
        """在独占文件锁内删除目标 thread 的 JSON 本子。"""
        validated = _validate_thread_id(thread_id)
        await asyncio.to_thread(self._delete_sync, validated)

    async def list_snapshots(self) -> tuple[ThreadPermissionsSnapshot, ...]:
        """枚举并严格读取所有已物化本子，v1 文件会逐个安全迁移。"""
        return await asyncio.to_thread(self._list_snapshots_sync)

    async def rollback_to_v1(self, thread_id: str) -> tuple[str, ...]:
        """把 v2 文件安全降级为 v1，丢弃 scoped Shell allow 并返回损失清单。"""
        validated = _validate_thread_id(thread_id)
        return await asyncio.to_thread(self._rollback_to_v1_sync, validated)

    def path_for(self, thread_id: str) -> Path:
        """返回 SHA-256 映射后的 JSON 路径，供 Manager 诊断和测试。"""
        validated = _validate_thread_id(thread_id)
        digest = hashlib.sha256(validated.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.json"

    def backup_path_for(self, thread_id: str) -> Path:
        """返回 v1 原文备份路径；后缀避开 ``*.json`` 的活跃文件扫描。"""
        return self.path_for(thread_id).with_suffix(".v1.bak")

    def _lock_path_for(self, thread_id: str) -> Path:
        """返回与 JSON 同 hash 的跨进程锁文件路径。"""
        return self.path_for(thread_id).with_suffix(".lock")

    def _read_sync(self, thread_id: str) -> ThreadPermissionsSnapshot:
        """同步锁定读取；缺文件返回 revision=0 的 schema v2 空本子。"""
        self._root.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self._lock_path_for(thread_id)):
            return self._read_unlocked(thread_id)

    def _read_unlocked(self, thread_id: str) -> ThreadPermissionsSnapshot:
        """读取锁内文件，严格区分 v1 迁移和 v2 正常路径。"""
        path = self.path_for(thread_id)
        if not path.exists():
            return _empty_snapshot(thread_id)
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("permissions payload must be an object")
            schema_version = payload.get("schema_version")
            if schema_version == 1:
                stored_v1 = _StoredPermissionsV1.model_validate(payload)
                _require_stored_identity(path, thread_id, stored_v1.thread_id)
                return self._migrate_v1_unlocked(path, raw, stored_v1)
            if schema_version == _SCHEMA_VERSION:
                stored = _StoredPermissions.model_validate(payload)
                _require_stored_identity(path, thread_id, stored.thread_id)
                return _snapshot_from_stored(stored)
            raise ValueError(f"unsupported permissions schema_version: {schema_version!r}")
        except OSError as exc:
            raise PermissionsStoreError(f"failed to read permissions file {path}: {exc}") from exc
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise PermissionsDataError(f"invalid permissions file {path}: {exc}") from exc

    def _migrate_v1_unlocked(
        self,
        path: Path,
        raw: str,
        stored: _StoredPermissionsV1,
    ) -> ThreadPermissionsSnapshot:
        """在当前独占锁内备份并把合法 v1 原子迁移为 v2。"""
        backup_path = self.backup_path_for(stored.thread_id)
        _write_backup_once(backup_path, raw)
        allow: list[PermissionRuleRecord] = []
        invalidated_shell_allow_count = 0
        for expression in stored.allow:
            matcher = parse_rule_expression(expression)
            if matcher.kind is MatcherKind.SHELL_PREFIX:
                invalidated_shell_allow_count += 1
                continue
            allow.append(PermissionRuleRecord(expression=expression))
        deny = [PermissionRuleRecord(expression=expression) for expression in stored.deny]
        migrated = ThreadPermissionsSnapshot(
            thread_id=stored.thread_id,
            revision=stored.revision + 1,
            allow=tuple(allow),
            deny=tuple(deny),
            updated_at=_utc_now_text(),
            schema_version=_SCHEMA_VERSION,
            migration_summary=PermissionsMigrationSummary(
                from_schema_version=1,
                to_schema_version=2,
                invalidated_shell_allow_count=invalidated_shell_allow_count,
                backup_path=backup_path.as_posix(),
            ),
        )
        encoded = _stored_from_snapshot(migrated)
        _StoredPermissions.model_validate(encoded.model_dump(mode="json"))
        _atomic_write_json(path, encoded)
        return migrated

    def _write_sync(
        self,
        thread_id: str,
        allow: tuple[PermissionRuleRecord, ...],
        deny: tuple[PermissionRuleRecord, ...],
        expected_revision: int,
    ) -> ThreadPermissionsSnapshot:
        """同步执行带跨进程锁的 CAS 写入。"""
        self._root.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self._lock_path_for(thread_id)):
            current = self._read_unlocked(thread_id)
            if current.revision != expected_revision:
                raise PermissionsRevisionConflict(
                    thread_id=thread_id,
                    expected_revision=expected_revision,
                    actual_revision=current.revision,
                )
            updated = ThreadPermissionsSnapshot(
                thread_id=thread_id,
                revision=current.revision + 1,
                allow=allow,
                deny=deny,
                updated_at=_utc_now_text(),
                schema_version=_SCHEMA_VERSION,
            )
            stored = _stored_from_snapshot(updated)
            _atomic_write_json(self.path_for(thread_id), stored)
            return updated

    def _delete_sync(self, thread_id: str) -> None:
        """同步锁定并删除 JSON；缺文件视为幂等成功。"""
        self._root.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self._lock_path_for(thread_id)):
            path = self.path_for(thread_id)
            try:
                path.unlink(missing_ok=True)
                _fsync_directory(self._root)
            except OSError as exc:
                raise PermissionsStoreError(
                    f"failed to delete permissions file {path}: {exc}"
                ) from exc

    def _list_snapshots_sync(self) -> tuple[ThreadPermissionsSnapshot, ...]:
        """同步扫描活跃 JSON 文件，并复用单 thread 锁定读取路径。"""
        if not self._root.exists():
            return ()
        try:
            paths = sorted(self._root.glob("*.json"))
        except OSError as exc:
            raise PermissionsStoreError(
                f"failed to list permissions directory {self._root}: {exc}"
            ) from exc
        snapshots: list[ThreadPermissionsSnapshot] = []
        for path in paths:
            with _exclusive_file_lock(path.with_suffix(".lock")):
                if not path.exists():
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    thread_id = _validate_thread_id(payload["thread_id"])
                except (OSError, KeyError, TypeError, ValueError) as exc:
                    raise PermissionsDataError(f"invalid permissions file {path}: {exc}") from exc
                if self.path_for(thread_id) != path:
                    raise PermissionsDataError(
                        f"permissions file hash mismatch at {path} for {thread_id!r}"
                    )
                snapshots.append(self._read_unlocked(thread_id))
        return tuple(snapshots)

    def _rollback_to_v1_sync(self, thread_id: str) -> tuple[str, ...]:
        """锁内写回 v1；返回被丢弃的 scoped Shell allow 表达式。"""
        self._root.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self._lock_path_for(thread_id)):
            snapshot = self._read_unlocked(thread_id)
            lost = tuple(
                record.expression
                for record in snapshot.allow
                if parse_rule_expression(record.expression).kind is MatcherKind.SHELL_PREFIX
            )
            v1_allow = [
                record.expression
                for record in snapshot.allow
                if parse_rule_expression(record.expression).kind is not MatcherKind.SHELL_PREFIX
            ]
            payload = _StoredPermissionsV1(
                schema_version=1,
                thread_id=thread_id,
                revision=snapshot.revision + 1,
                allow=v1_allow,
                deny=[record.expression for record in snapshot.deny],
                updated_at=_utc_now_text(),
            )
            _atomic_write_json(self.path_for(thread_id), payload)
            return lost


def _stored_from_snapshot(snapshot: ThreadPermissionsSnapshot) -> _StoredPermissions:
    """把领域快照投影为严格磁盘模型。"""
    return _StoredPermissions(
        schema_version=_SCHEMA_VERSION,
        thread_id=snapshot.thread_id,
        revision=snapshot.revision,
        allow=[
            _StoredPermissionRule(expression=item.expression, scope_cwd=item.scope_cwd)
            for item in snapshot.allow
        ],
        deny=[
            _StoredPermissionRule(expression=item.expression, scope_cwd=item.scope_cwd)
            for item in snapshot.deny
        ],
        updated_at=snapshot.updated_at,
    )


def _snapshot_from_stored(stored: _StoredPermissions) -> ThreadPermissionsSnapshot:
    """把严格磁盘模型投影为不可变领域快照。"""
    return ThreadPermissionsSnapshot(
        thread_id=stored.thread_id,
        revision=stored.revision,
        allow=tuple(
            PermissionRuleRecord(expression=item.expression, scope_cwd=item.scope_cwd)
            for item in stored.allow
        ),
        deny=tuple(
            PermissionRuleRecord(expression=item.expression, scope_cwd=item.scope_cwd)
            for item in stored.deny
        ),
        updated_at=stored.updated_at,
        schema_version=stored.schema_version,
    )


def _validate_rule_scope(record: PermissionRuleRecord, verdict: Verdict) -> None:
    """校验 matcher 与 scope 组合，Shell allow 强制 exact cwd。"""
    matcher = parse_rule_expression(record.expression)
    if matcher.kind is MatcherKind.SHELL_PREFIX:
        if verdict is Verdict.ALLOW and record.scope_cwd is None:
            raise ValueError("shell allow rule requires scope_cwd")
        if record.scope_cwd is not None:
            _canonical_scope_cwd(record.scope_cwd)
        return
    if record.scope_cwd is not None:
        raise ValueError("non-shell rule must use scope_cwd=null")


def _canonical_scope_cwd(value: str) -> str:
    """校验 scope 为 canonical absolute POSIX path，目录可暂时不存在。"""
    if not value or value != value.strip():
        raise ValueError("scope_cwd must be non-empty canonical text")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("scope_cwd must be absolute")
    canonical = path.resolve(strict=False).as_posix()
    if canonical != value:
        raise ValueError("scope_cwd must already be canonical")
    return value


def _require_stored_identity(path: Path, expected: str, actual: str) -> None:
    """阻止 hash 文件内容冒充另一个 thread。"""
    if actual != expected:
        raise PermissionsDataError(
            f"permissions file identity mismatch at {path}: expected {expected!r}, found {actual!r}"
        )


def _validate_updated_at(value: str | None) -> str | None:
    """校验可选的带时区 ISO-8601 时间。"""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("updated_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("updated_at must include a timezone")
    return value


def _validate_thread_id(thread_id: str) -> str:
    """校验稳定身份文本；路径字符由 SHA-256 文件名隔离。"""
    if not isinstance(thread_id, str):
        raise TypeError("thread_id must be a string")
    if not thread_id or thread_id != thread_id.strip():
        raise ValueError("thread_id must be non-empty canonical text")
    if len(thread_id) > _MAX_THREAD_ID_CHARS:
        raise ValueError(f"thread_id must contain at most {_MAX_THREAD_ID_CHARS} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in thread_id):
        raise ValueError("thread_id must not contain control characters")
    return thread_id


def _empty_snapshot(thread_id: str) -> ThreadPermissionsSnapshot:
    """构造尚未物化文件的 schema v2 空本子。"""
    return ThreadPermissionsSnapshot(
        thread_id=thread_id,
        revision=0,
        allow=(),
        deny=(),
        updated_at=None,
        schema_version=_SCHEMA_VERSION,
    )


def _utc_now_text() -> str:
    """返回秒级 UTC ISO-8601 文本。"""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextlib.contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """持有单个 thread 的跨进程独占文件锁。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        _prime_lock_file(handle.fileno())
        if _IS_WINDOWS:
            import msvcrt

            os.lseek(handle.fileno(), 0, os.SEEK_SET)
            msvcrt.locking(  # type: ignore[attr-defined]
                handle.fileno(),
                _WINDOWS_LOCK_MODE,
                1,
            )
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if _IS_WINDOWS:
                import msvcrt

                os.lseek(handle.fileno(), 0, os.SEEK_SET)
                msvcrt.locking(  # type: ignore[attr-defined]
                    handle.fileno(),
                    _WINDOWS_UNLOCK_MODE,
                    1,
                )
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _prime_lock_file(fd: int) -> None:
    """为 Windows 字节区间锁准备一个稳定字节。"""
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"0")
    os.lseek(fd, 0, os.SEEK_SET)


def _write_backup_once(path: Path, content: str) -> None:
    """首次迁移时以排他创建方式保存 v1 原文并 fsync。"""
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") != content:
                raise PermissionsStoreError(
                    f"existing permissions backup differs from active v1 file: {path}"
                )
        except OSError as exc:
            raise PermissionsStoreError(
                f"failed to verify permissions backup {path}: {exc}"
            ) from exc
        return
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except FileExistsError:
        _write_backup_once(path, content)
    except OSError as exc:
        raise PermissionsStoreError(f"failed to backup permissions file {path}: {exc}") from exc


def _atomic_write_json(path: Path, stored: BaseModel) -> None:
    """写临时 JSON、重新解析校验、fsync 文件并原子替换目标。"""
    payload = stored.model_dump(mode="json")
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with tmp_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        json.loads(tmp_path.read_text(encoding="utf-8"))
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    except (OSError, ValueError) as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise PermissionsStoreError(f"failed to write permissions file {path}: {exc}") from exc


def _fsync_directory(path: Path) -> None:
    """在支持目录 fsync 的平台提交目录项更新。"""
    if _IS_WINDOWS:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = ["ThreadPermissionsStore"]
