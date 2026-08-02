"""Thread permissions 的 Web REST 门户。

本模块位于 Web 装配边界，负责把 safety 层快照投影为严格 REST DTO，并把
底层 revision 冲突翻译成 Web thread 子域错误。路由只依赖 Web Protocol，
不会直接接触 safety 的持久化类型。

关键入口：

- ``ThreadPermissionsRestManager.get``：读取指定 thread 的本子快照。
- ``ThreadPermissionsRestManager.replace``：按 revision CAS 整本替换本子。
"""

from __future__ import annotations

from hosts.web.protocol import (
    PermissionRuleDTO,
    PermissionsMigrationSummaryDTO,
    ThreadPermissionsDTO,
)
from hosts.web.threads.errors import (
    ThreadPermissionsRevisionConflictError,
    ThreadPermissionsStorageError,
    ThreadPermissionsValidationError,
)
from safety.approval.permissions_errors import (
    PermissionsDataError,
    PermissionsExpressionError,
    PermissionsRevisionConflict,
    PermissionsStoreError,
)
from safety.approval.permissions_manager import PermissionsManager
from safety.approval.rule_models import PermissionRuleRecord, ThreadPermissionsSnapshot


class ThreadPermissionsRestManager:
    """向 Web 路由暴露 thread permissions 的 DTO 级门户。"""

    def __init__(self, permissions_manager: PermissionsManager) -> None:
        """绑定安全域的唯一 permissions 门户。"""
        self._permissions_manager = permissions_manager

    @staticmethod
    def _to_dto(snapshot: ThreadPermissionsSnapshot) -> ThreadPermissionsDTO:
        """把安全域不可变快照投影为 Web REST DTO。"""
        return ThreadPermissionsDTO(
            thread_id=snapshot.thread_id,
            revision=snapshot.revision,
            allow=[
                PermissionRuleDTO(
                    expression=record.expression,
                    scope_cwd=record.scope_cwd,
                )
                for record in snapshot.allow
            ],
            deny=[
                PermissionRuleDTO(
                    expression=record.expression,
                    scope_cwd=record.scope_cwd,
                )
                for record in snapshot.deny
            ],
            updated_at=snapshot.updated_at,
            migration_summary=(
                PermissionsMigrationSummaryDTO(
                    from_schema_version=1,
                    to_schema_version=2,
                    invalidated_shell_allow_count=(
                        snapshot.migration_summary.invalidated_shell_allow_count
                    ),
                    backup_path=snapshot.migration_summary.backup_path,
                )
                if snapshot.migration_summary is not None
                else None
            ),
        )

    async def get(self, thread_id: str) -> ThreadPermissionsDTO:
        """读取指定 thread 的 permissions 快照。"""
        try:
            snapshot = await self._permissions_manager.snapshot(thread_id)
        except (PermissionsDataError, PermissionsStoreError) as exc:
            raise ThreadPermissionsStorageError(
                thread_id=thread_id,
                message=str(exc),
            ) from exc
        return self._to_dto(snapshot)

    async def replace(
        self,
        thread_id: str,
        *,
        allow: list[PermissionRuleDTO],
        deny: list[PermissionRuleDTO],
        expected_revision: int,
    ) -> ThreadPermissionsDTO:
        """按 revision CAS 替换 permissions，并稳定翻译冲突错误。"""
        try:
            snapshot = await self._permissions_manager.replace(
                thread_id,
                allow=[
                    PermissionRuleRecord(
                        expression=record.expression,
                        scope_cwd=record.scope_cwd,
                    )
                    for record in allow
                ],
                deny=[
                    PermissionRuleRecord(
                        expression=record.expression,
                        scope_cwd=record.scope_cwd,
                    )
                    for record in deny
                ],
                expected_revision=expected_revision,
            )
        except PermissionsRevisionConflict as exc:
            raise ThreadPermissionsRevisionConflictError(
                thread_id=thread_id,
                expected_revision=exc.expected_revision,
                actual_revision=exc.actual_revision,
            ) from exc
        except PermissionsExpressionError as exc:
            raise ThreadPermissionsValidationError(
                thread_id=thread_id,
                message=str(exc),
            ) from exc
        except (PermissionsDataError, PermissionsStoreError) as exc:
            raise ThreadPermissionsStorageError(
                thread_id=thread_id,
                message=str(exc),
            ) from exc
        return self._to_dto(snapshot)


__all__ = ["ThreadPermissionsRestManager"]
