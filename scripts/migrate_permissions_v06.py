#!/usr/bin/env python3
"""把旧全局 safety 规则定向迁移到一个 thread permissions 本子。

脚本执行流程：

1. 读取旧 ``setting.yaml`` 中 permissions、approval_rules、allow/deny、
   allow_writes、allow_tools_silent 与 trusted_workdirs；
2. allow/deny 与启用的 approval_rules 定向合并到显式 ``--thread-id``，
   无法证明 cwd 的旧 Shell allow 安全失效；
3. ask 规则丢弃，trusted_workdirs 只生成候选表达式并展示；
4. ``--dry-run`` 只打印 diff，``--apply`` 通过 PermissionsManager 的 revision
   CAS 整本替换写入；
5. 全流程不枚举 thread，也不把一份全局授权扇出到其他 thread。

关键函数：

- :func:`extract_legacy_rules`：解析历史结构并生成规范化迁移输入；
- :func:`migrate_permissions`：读取目标快照、计算 diff，并按需调用 Manager；
- :func:`render_report`：输出 allow/deny diff、ask 丢弃数与 trusted 候选；
- :func:`main`：处理命令行参数和退出码。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from infrastructure.config.paths import get_kongming_home  # noqa: E402
from safety.approval.permissions_errors import PermissionsError  # noqa: E402
from safety.approval.permissions_manager import PermissionsManager  # noqa: E402
from safety.approval.rule_models import (  # noqa: E402
    MatcherKind,
    PermissionRuleRecord,
    ThreadPermissionsSnapshot,
)
from safety.approval.rule_parser import parse_rule_expression  # noqa: E402


class MigrationInputError(ValueError):
    """旧配置结构或表达式无法安全迁移时返回的输入错误。"""


@dataclass(frozen=True)
class LegacyRules:
    """从旧 setting 提取出的规范化迁移输入。"""

    allow: tuple[str, ...]
    deny: tuple[str, ...]
    discarded_ask: int
    trusted_candidates: tuple[str, ...]


@dataclass(frozen=True)
class PermissionsDiff:
    """目标 thread 迁移前后 permissions 的稳定 diff。"""

    allow_added: tuple[str, ...]
    allow_removed: tuple[str, ...]
    deny_added: tuple[str, ...]
    deny_removed: tuple[str, ...]

    @property
    def changed(self) -> bool:
        """返回迁移计划是否会改变目标本子。"""
        return any(
            (
                self.allow_added,
                self.allow_removed,
                self.deny_added,
                self.deny_removed,
            )
        )


@dataclass(frozen=True)
class MigrationResult:
    """一次 dry-run 或 apply 的完整结果。"""

    thread_id: str
    before: ThreadPermissionsSnapshot
    after: ThreadPermissionsSnapshot
    diff: PermissionsDiff
    discarded_ask: int
    trusted_candidates: tuple[str, ...]
    invalidated_shell_allow_count: int
    applied: bool


def load_legacy_setting(path: Path) -> dict[str, object]:
    """读取旧 setting YAML 并返回字符串键映射。"""
    try:
        raw = path.read_text(encoding="utf-8")
        payload = yaml.safe_load(raw)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise MigrationInputError(f"无法读取旧配置 {path}: {exc}") from exc
    if payload is None:
        return {}
    return _string_mapping(payload, field="setting.yaml")


def extract_legacy_rules(
    setting: Mapping[str, object],
    *,
    project_root: Path,
) -> LegacyRules:
    """解析所有支持的旧 safety 结构并生成 canonical DSL。"""
    safety_raw = setting.get("safety", {})
    safety = _string_mapping(safety_raw, field="safety")
    allow: list[str] = []
    deny: list[str] = []
    discarded_ask = 0

    permissions_raw = safety.get("permissions")
    if permissions_raw is not None:
        permissions = _string_mapping(permissions_raw, field="safety.permissions")
        allow.extend(
            _expression_list(permissions.get("allow", []), field="safety.permissions.allow")
        )
        deny.extend(_expression_list(permissions.get("deny", []), field="safety.permissions.deny"))

    allow.extend(_expression_list(safety.get("allow", []), field="safety.allow"))
    deny.extend(_expression_list(safety.get("deny", []), field="safety.deny"))

    approval_rules = _object_list(
        safety.get("approval_rules", []),
        field="safety.approval_rules",
    )
    for index, raw_rule in enumerate(approval_rules):
        rule = _string_mapping(raw_rule, field=f"safety.approval_rules[{index}]")
        if rule.get("enabled", True) is False:
            continue
        behavior = rule.get("behavior")
        expression = rule.get("expression")
        if behavior not in {"allow", "deny", "ask"}:
            raise MigrationInputError(
                f"safety.approval_rules[{index}].behavior 必须是 allow/deny/ask"
            )
        if not isinstance(expression, str):
            raise MigrationInputError(f"safety.approval_rules[{index}].expression 必须是字符串")
        if behavior == "ask":
            discarded_ask += 1
        elif behavior == "allow":
            allow.append(_canonical_expression(expression))
        else:
            deny.append(_canonical_expression(expression))

    allow.extend(
        _expression_list(
            safety.get("allow_tools_silent", []),
            field="safety.allow_tools_silent",
        )
    )
    allow.extend(
        _path_expressions(
            safety.get("allow_writes", []),
            tools=("write_file", "edit_file"),
            project_root=project_root,
            field="safety.allow_writes",
        )
    )

    trusted_candidates = _path_expressions(
        safety.get("trusted_workdirs", []),
        tools=("read_file", "list_dir", "write_file", "edit_file"),
        project_root=project_root,
        field="safety.trusted_workdirs",
    )
    return LegacyRules(
        allow=_dedupe(allow),
        deny=_dedupe(deny),
        discarded_ask=discarded_ask,
        trusted_candidates=_dedupe(trusted_candidates),
    )


async def migrate_permissions(
    *,
    config_path: Path,
    kongming_home: Path,
    project_root: Path,
    thread_id: str,
    apply: bool,
    manager: PermissionsManager | None = None,
) -> MigrationResult:
    """为单个显式 thread 计算迁移 diff，并按需通过 Manager 写入。"""
    setting = load_legacy_setting(config_path)
    legacy = extract_legacy_rules(setting, project_root=project_root)
    portal = manager or PermissionsManager(kongming_home)
    before = await portal.snapshot(thread_id)

    legacy_deny = tuple(PermissionRuleRecord(expression=item) for item in legacy.deny)
    planned_deny = _dedupe_records((*before.deny, *legacy_deny))
    deny_expressions = frozenset(record.expression for record in planned_deny)
    legacy_safe_allow = tuple(
        PermissionRuleRecord(expression=expression)
        for expression in legacy.allow
        if parse_rule_expression(expression).kind is not MatcherKind.SHELL_PREFIX
    )
    invalidated_shell_allow_count = len(legacy.allow) - len(legacy_safe_allow)
    planned_allow = tuple(
        record
        for record in _dedupe_records((*before.allow, *legacy_safe_allow))
        if record.expression not in deny_expressions
    )
    diff = _build_diff(
        before,
        planned_allow=planned_allow,
        planned_deny=planned_deny,
    )

    after = before
    applied = False
    if apply and diff.changed:
        after = await portal.replace(
            thread_id,
            allow=planned_allow,
            deny=planned_deny,
            expected_revision=before.revision,
        )
        applied = True
    return MigrationResult(
        thread_id=thread_id,
        before=before,
        after=after,
        diff=diff,
        discarded_ask=legacy.discarded_ask,
        trusted_candidates=legacy.trusted_candidates,
        invalidated_shell_allow_count=invalidated_shell_allow_count,
        applied=applied,
    )


def render_report(result: MigrationResult, *, dry_run: bool) -> str:
    """渲染单 thread diff、丢弃项和候选表达式。"""
    mode = "DRY RUN" if dry_run else "APPLY"
    planned_revision = result.before.revision + int(result.diff.changed)
    lines = [
        f"permissions v0.6 migration [{mode}]",
        f"thread_id: {result.thread_id}",
        (
            f"revision: {result.before.revision} -> "
            f"{result.after.revision if result.applied else planned_revision}"
        ),
        f"changed: {str(result.diff.changed).lower()}",
        f"allow: +{len(result.diff.allow_added)} -{len(result.diff.allow_removed)}",
    ]
    lines.extend(f"  + {item}" for item in result.diff.allow_added)
    lines.extend(f"  - {item}" for item in result.diff.allow_removed)
    lines.append(f"deny: +{len(result.diff.deny_added)} -{len(result.diff.deny_removed)}")
    lines.extend(f"  + {item}" for item in result.diff.deny_added)
    lines.extend(f"  - {item}" for item in result.diff.deny_removed)
    lines.append(f"discarded ask rules: {result.discarded_ask}")
    lines.append(f"invalidated unscoped shell allow rules: {result.invalidated_shell_allow_count}")
    lines.append(f"trusted_workdirs candidates: {len(result.trusted_candidates)}")
    lines.extend(f"  ? {item}" for item in result.trusted_candidates)
    if dry_run:
        lines.append("dry-run: target thread was not written")
    elif not result.applied:
        lines.append("apply: no changes; target thread already up to date")
    else:
        lines.append("apply: target thread updated through PermissionsManager CAS")
    return "\n".join(lines)


def _build_diff(
    before: ThreadPermissionsSnapshot,
    *,
    planned_allow: tuple[PermissionRuleRecord, ...],
    planned_deny: tuple[PermissionRuleRecord, ...],
) -> PermissionsDiff:
    """按稳定列表顺序计算 allow/deny 增删集合。"""
    before_allow_text = tuple(_display_record(item) for item in before.allow)
    before_deny_text = tuple(_display_record(item) for item in before.deny)
    before_allow = frozenset(before_allow_text)
    before_deny = frozenset(before_deny_text)
    planned_allow_text = tuple(_display_record(item) for item in planned_allow)
    planned_deny_text = tuple(_display_record(item) for item in planned_deny)
    planned_allow_set = frozenset(planned_allow_text)
    planned_deny_set = frozenset(planned_deny_text)
    return PermissionsDiff(
        allow_added=tuple(item for item in planned_allow_text if item not in before_allow),
        allow_removed=tuple(item for item in before_allow_text if item not in planned_allow_set),
        deny_added=tuple(item for item in planned_deny_text if item not in before_deny),
        deny_removed=tuple(item for item in before_deny_text if item not in planned_deny_set),
    )


def _dedupe_records(
    records: Iterable[PermissionRuleRecord],
) -> tuple[PermissionRuleRecord, ...]:
    """按 expression + scope_cwd 保留结构化规则首次出现顺序。"""
    result: list[PermissionRuleRecord] = []
    seen: set[tuple[str, str | None]] = set()
    for record in records:
        identity = (record.expression, record.scope_cwd)
        if identity not in seen:
            result.append(record)
            seen.add(identity)
    return tuple(result)


def _display_record(record: PermissionRuleRecord) -> str:
    """把结构化规则渲染为稳定 diff 文本。"""
    if record.scope_cwd is None:
        return record.expression
    return f"{record.expression} @ {record.scope_cwd}"


def _expression_list(value: object, *, field: str) -> tuple[str, ...]:
    """校验表达式数组并 canonicalize 每一项。"""
    values = _string_list(value, field=field)
    return tuple(_canonical_expression(expression) for expression in values)


def _canonical_expression(expression: str) -> str:
    """解析历史表达式并返回 v0.6 canonical DSL。"""
    try:
        return parse_rule_expression(expression).canonical_expression
    except ValueError as exc:
        raise MigrationInputError(f"无效 permission 表达式 {expression!r}: {exc}") from exc


def _path_expressions(
    value: object,
    *,
    tools: Sequence[str],
    project_root: Path,
    field: str,
) -> tuple[str, ...]:
    """把历史路径列表转为指定工具的 canonical path_prefix 表达式。"""
    result: list[str] = []
    for raw_path in _string_list(value, field=field):
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = project_root / path
        canonical_path = path.resolve(strict=False).as_posix()
        for tool in tools:
            result.append(_canonical_expression(f"{tool}({canonical_path}/**)"))
    return tuple(result)


def _string_mapping(value: object, *, field: str) -> dict[str, object]:
    """校验 YAML 节点为字符串键 mapping。"""
    if not isinstance(value, Mapping):
        raise MigrationInputError(f"{field} 必须是 mapping")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise MigrationInputError(f"{field} 只能使用字符串键")
        result[key] = item
    return result


def _object_list(value: object, *, field: str) -> tuple[object, ...]:
    """校验 YAML 节点为非字符串 list。"""
    if not isinstance(value, list):
        raise MigrationInputError(f"{field} 必须是 list")
    return tuple(value)


def _string_list(value: object, *, field: str) -> tuple[str, ...]:
    """校验 YAML 节点为非空字符串数组。"""
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise MigrationInputError(f"{field} 必须是非空字符串 list")
    return tuple(item.strip() for item in value if isinstance(item, str))


def _dedupe(expressions: Iterable[str]) -> tuple[str, ...]:
    """按首次出现顺序对表达式去重。"""
    return tuple(dict.fromkeys(expressions))


def _infer_project_root(config_path: Path) -> Path:
    """从 repo config 或用户 setting 路径推导历史相对路径基准。"""
    parent = config_path.expanduser().resolve(strict=False).parent
    return parent.parent if parent.name.casefold() == "config" else parent


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析必须显式 thread 与 dry-run/apply 模式的 CLI 参数。"""
    parser = argparse.ArgumentParser(
        description="把旧全局 safety rules 定向迁移到一个 thread permissions 本子。"
    )
    parser.add_argument("--thread-id", required=True, help="唯一目标 thread id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只打印 diff，不写盘")
    mode.add_argument("--apply", action="store_true", help="经 PermissionsManager CAS 写入")
    parser.add_argument(
        "--config",
        type=Path,
        default=_PROJECT_ROOT / "config" / "setting.yaml",
        help="旧 setting.yaml 路径",
    )
    parser.add_argument("--kongming-home", type=Path, default=None, help="Kongming home")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="历史相对路径基准；默认按 setting.yaml 位置推导",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """运行定向迁移并打印报告；输入或持久化错误返回退出码 2。"""
    args = _parse_args(argv)
    config_path = args.config.expanduser().resolve(strict=False)
    kongming_home = (
        args.kongming_home.expanduser().resolve(strict=False)
        if args.kongming_home is not None
        else get_kongming_home()
    )
    project_root = (
        args.project_root.expanduser().resolve(strict=False)
        if args.project_root is not None
        else _infer_project_root(config_path)
    )
    try:
        result = asyncio.run(
            migrate_permissions(
                config_path=config_path,
                kongming_home=kongming_home,
                project_root=project_root,
                thread_id=args.thread_id,
                apply=bool(args.apply),
            )
        )
    except (MigrationInputError, PermissionsError, OSError, ValueError) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 2
    print(render_report(result, dry_run=bool(args.dry_run)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
