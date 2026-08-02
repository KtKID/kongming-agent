"""统一识别任何审批模式都必须交给用户确认的危险操作。

本模块把既有 HardBlock 与 destructive 写死规则收敛为一个只读匹配器，并新增
仓库内部文件、agent 指令和安全策略自提权检测。关键职责如下：

- :class:`DangerGuard` 通过 :meth:`DangerGuard.match` 返回第一条危险命中；
- :class:`DangerRule` 以不可变值对象携带规则名、原因、目标和审计建议；
- shell 命令先做整句匹配，再按连接符拆段，覆盖组合命令中的危险片段；
- 安全策略写入只检查扩大权限的内容，普通 deny 本子写入仍由正常审批链处理。

本模块只负责识别危险操作。调用方负责把命中转换为 ``danger=true``、
``remember_allowed=false`` 的强制人工审批。
"""

from __future__ import annotations

import ast
import json
import re
import shlex
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from core.contracts import ApprovalRequest
from safety._request_context import SafetyRequestContext
from safety.approval.default_rules import (
    DEFAULT_DESTRUCTIVE_ALWAYS_ASK,
    DEFAULT_HARD_DENY_COMMANDS,
    DEFAULT_SENSITIVE_PATHS,
    DangerCommandRule,
    DangerPathRule,
)
from safety.approval.rule_models import MatcherKind
from safety.approval.rule_parser import parse_rule_expression

_READ_TOOLS: frozenset[str] = frozenset({"read_file", "list_dir"})
_WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file"})
_SHELL_TOOLS: frozenset[str] = frozenset({"run_shell"})
_PATH_KEYS: tuple[str, ...] = ("path", "file_path")
_CONTENT_KEYS: tuple[str, ...] = ("content", "new_string", "replacement", "new_text")
_SHELL_SEGMENT_SPLITTER = re.compile(r"(?:&&|\|\||;|\|)")
_ALLOW_LIST_RE = re.compile(
    r"(?:^|[,{\s])[\"']?allow[\"']?\s*:\s*\[(?P<body>[^\]]*)\]",
    flags=re.IGNORECASE | re.DOTALL,
)
_PYTHON_FILE_WRITE_RE = re.compile(
    r"\b(?:py|python(?:3(?:\.\d+)?)?)\b.*?\bopen\s*\([^)]*,\s*[\"']?[wax+]",
    flags=re.IGNORECASE | re.DOTALL,
)
_PYTHON_PATH_WRITE_RE = re.compile(
    r"\b(?:pathlib\.)?Path\s*\([^)]*\)\s*\.\s*write_(?:bytes|text)\s*\(",
    flags=re.IGNORECASE | re.DOTALL,
)
_POWERSHELL_MUTATION_RE = re.compile(
    (
        r"\b(?:add-content|clear-content|copy-item|move-item|new-item|out-file|"
        r"remove-item|set-content)\b"
    ),
    flags=re.IGNORECASE,
)
_DD_FILE_WRITE_RE = re.compile(r"\bdd\b[^\r\n]*(?:^|\s)of\s*=", flags=re.IGNORECASE)
_SHELL_MUTATION_COMMANDS: frozenset[str] = frozenset(
    {
        "add-content",
        "chmod",
        "chown",
        "clear-content",
        "copy",
        "copy-item",
        "cp",
        "del",
        "erase",
        "install",
        "ln",
        "mkdir",
        "move",
        "move-item",
        "mv",
        "new-item",
        "remove-item",
        "ren",
        "rename",
        "rmdir",
        "rm",
        "set-content",
        "tee",
        "touch",
        "truncate",
    }
)
_ROOT_LIKE_PATH_PREFIXES: tuple[str, ...] = (
    "/",
    "/etc",
    "/usr",
    "/var",
    "/bin",
    "/sbin",
    "/root",
    "~",
    "$HOME",
)


class DangerTargetKind(StrEnum):
    """危险命中的目标类型，供审批审计稳定分类。"""

    COMMAND = "command"
    PATH = "path"
    SAFETY_POLICY = "safety_policy"


class DangerAction(StrEnum):
    """危险命中在决策链中的固定处置等级。"""

    BLOCK = "block"
    ELEVATED = "elevated"
    FORCE_ASK = "force_ask"


@dataclass(frozen=True)
class DangerRule:
    """一次危险命中的不可变审计值对象。"""

    name: str
    reason: str
    matcher: str
    target_kind: DangerTargetKind
    target_value: str
    action: DangerAction = DangerAction.BLOCK
    suggested_alternatives: tuple[str, ...] = ()


class DangerGuard:
    """匹配所有审批模式之前执行的写死危险规则。"""

    def __init__(self, *, kongming_home: Path) -> None:
        """绑定运行数据根并预编译默认命令规则。"""
        self._kongming_home = kongming_home.expanduser().resolve(strict=False)
        self._prompts_root = (self._kongming_home / "prompts").resolve(strict=False)
        self._thread_permissions_root = (
            self._kongming_home / "safety" / "thread_permissions"
        ).resolve(strict=False)
        self._hard_commands = _compile_hard_commands(DEFAULT_HARD_DENY_COMMANDS)
        self._destructive_commands = _compile_destructive_commands(DEFAULT_DESTRUCTIVE_ALWAYS_ASK)
        self._blocked_paths = _resolve_blocked_paths(DEFAULT_SENSITIVE_PATHS)

    def match(self, request: ApprovalRequest) -> DangerRule | None:
        """返回第一条危险命中；安全请求返回 ``None``。"""
        request_context = SafetyRequestContext.from_request(request)
        if request_context.missing_required_cwd:
            return DangerRule(
                name="shell-execution-scope-missing",
                reason="Shell 请求缺少合法的 prepared execution scope",
                matcher="run_shell:execution_scope.cwd",
                target_kind=DangerTargetKind.COMMAND,
                target_value=_request_command(request.arguments) or request.tool_name,
                action=DangerAction.BLOCK,
                suggested_alternatives=("重新准备 Shell 调用并提供明确的绝对执行目录",),
            )

        path_error = _match_path_resolution_failure(request)
        if path_error is not None:
            return path_error

        policy_hit = self._match_safety_policy_escalation(request)
        if policy_hit is not None:
            return policy_hit

        protected_write_hit = self._match_protected_write(request)
        if protected_write_hit is not None:
            return protected_write_hit

        path_hit = self._match_blocked_path(request)
        if path_hit is not None:
            return path_hit

        return self._match_shell_command(request)

    def _match_safety_policy_escalation(
        self,
        request: ApprovalRequest,
    ) -> DangerRule | None:
        """识别扩大 thread allow 等可改变审批边界的自提权写入。"""
        raw_path = _request_path(request.arguments)
        if request.tool_name in _WRITE_TOOLS and raw_path is not None:
            target = _resolve_request_path(raw_path, request)
            if _is_under(target, self._thread_permissions_root) and _expands_thread_allow(
                request.arguments,
                target=target,
            ):
                return _policy_rule(
                    reason="扩大 thread permissions allow 会放宽后续工具权限",
                    matcher="thread_permissions:allow-expansion",
                    target=target,
                )
            legacy_config_target = (
                target.name.casefold() == "setting.yaml"
                or _is_legacy_kongming_config_target(target)
            )
            if legacy_config_target and _expands_legacy_global_allow_for_request(
                request,
                target=target,
            ):
                return _policy_rule(
                    reason="扩大旧全局 approval_rules allow 会放宽后续工具权限",
                    matcher="setting.yaml:approval_rules.allow",
                    target=target,
                )

        if request.tool_name not in _SHELL_TOOLS:
            return None
        command = _request_command(request.arguments)
        if command is None:
            return None
        for raw_target in _python_write_targets(command):
            try:
                target = _resolve_request_path(raw_target, request)
            except (OSError, RuntimeError, ValueError):
                continue
            if target.name.casefold() == "setting.yaml":
                return _policy_rule(
                    reason="Python 代码写入 setting.yaml，可能改变审批复核配置",
                    matcher="setting.yaml:safety-policy-write",
                    target=target,
                    target_value=command,
                )
            if _is_under(target, self._thread_permissions_root):
                return _policy_rule(
                    reason="Python 代码直接写入 thread permissions，内容无法安全比较",
                    matcher="thread_permissions:safety-policy-write",
                    target=target,
                    target_value=command,
                )
        normalized = _normalize_shell_text(command)
        if "setting.yaml" in normalized.casefold() and _shell_has_mutation(command):
            return _policy_rule(
                reason="shell 命令写入 setting.yaml，可能改变审批复核配置",
                matcher="setting.yaml:safety-policy-write",
                target=Path("setting.yaml"),
                target_value=command,
            )
        permissions_marker = _normalize_shell_text(str(self._thread_permissions_root)).casefold()
        if (
            permissions_marker in normalized.casefold()
            or "thread_permissions/" in normalized.casefold()
        ) and _shell_has_mutation(command):
            return _policy_rule(
                reason="shell 命令直接写入 thread permissions，内容无法安全比较",
                matcher="thread_permissions:safety-policy-write",
                target=self._thread_permissions_root,
                target_value=command,
            )
        if (
            _shell_targets_legacy_kongming_config(normalized)
            and "approval_rules" in normalized.casefold()
            and "allow" in normalized.casefold()
            and _shell_has_mutation(command)
        ):
            return _policy_rule(
                reason="shell 命令扩大旧全局 approval_rules allow",
                matcher="legacy-config:approval_rules.allow",
                target=Path(".kongming/config"),
                target_value=command,
            )
        return None

    def _match_protected_write(self, request: ApprovalRequest) -> DangerRule | None:
        """识别 .git HardBlock 与 agent 自我配置的分级写入。"""
        raw_path = _request_path(request.arguments)
        if request.tool_name in _WRITE_TOOLS and raw_path is not None:
            target = _resolve_request_path(raw_path, request)
            instruction_action = _instruction_write_action(request, target, self._prompts_root)
            if instruction_action is not None:
                return DangerRule(
                    name="agent-instruction-write",
                    reason="写入 agent 指令源会改变后续模型行为",
                    matcher="**/AGENTS.md|**/CLAUDE.md|<kongming_home>/prompts/**",
                    target_kind=DangerTargetKind.PATH,
                    target_value=str(target),
                    action=instruction_action,
                    suggested_alternatives=("展示修改内容并由用户显式确认",),
                )
            if _is_self_configuration_path(target):
                return DangerRule(
                    name="self-configuration-write",
                    reason="写入 .env 或 .kongming 配置会改变后续执行边界",
                    matcher="**/.env*|**/.kongming/**",
                    target_kind=DangerTargetKind.PATH,
                    target_value=str(target),
                    action=DangerAction.ELEVATED,
                    suggested_alternatives=("展示修改内容并由用户显式确认",),
                )

        if request.tool_name not in _SHELL_TOOLS:
            return None
        command = _request_command(request.arguments)
        if command is None:
            return None
        for raw_target in _python_write_targets(command):
            try:
                target = _resolve_request_path(raw_target, request)
            except (OSError, RuntimeError, ValueError):
                continue
            if _contains_git_component(target):
                return DangerRule(
                    name="git-internal",
                    reason="Python 代码写入 .git 内部文件会改变或破坏仓库状态",
                    matcher=".git/",
                    target_kind=DangerTargetKind.COMMAND,
                    target_value=command,
                    suggested_alternatives=("使用 git 命令完成仓库状态变更",),
                )
        for segment in _split_shell_segments(command):
            normalized = _normalize_shell_text(segment)
            if _shell_destroys_instruction_file(normalized):
                return DangerRule(
                    name="agent-instruction-destroy",
                    reason="删除或清空 agent 指令文件会移除行为约束",
                    matcher="rm|mv|> **/AGENTS.md|**/CLAUDE.md",
                    target_kind=DangerTargetKind.COMMAND,
                    target_value=segment.strip(),
                    action=DangerAction.BLOCK,
                    suggested_alternatives=("使用 edit_file 对明确片段进行受控编辑",),
                )
            if _shell_destroys_git_directory(normalized):
                return DangerRule(
                    name="git-dir-destroy",
                    reason="删除或移动 .git 目录会破坏仓库完整性",
                    matcher="rm|mv **/.git",
                    target_kind=DangerTargetKind.COMMAND,
                    target_value=segment.strip(),
                    action=DangerAction.BLOCK,
                    suggested_alternatives=("使用 git 命令完成仓库状态变更",),
                )
            if _shell_mutates_instruction_file(normalized):
                return DangerRule(
                    name="agent-instruction-write",
                    reason="修改 agent 指令文件会改变后续模型行为",
                    matcher="shell-write **/AGENTS.md|**/CLAUDE.md",
                    target_kind=DangerTargetKind.COMMAND,
                    target_value=segment.strip(),
                    action=DangerAction.ELEVATED,
                    suggested_alternatives=("展示修改内容并由用户显式确认",),
                )
            if _shell_mutates_self_configuration(normalized):
                return DangerRule(
                    name="self-configuration-write",
                    reason="修改 .env 或 .kongming 配置会改变后续执行边界",
                    matcher="shell-write **/.env*|**/.kongming/**",
                    target_kind=DangerTargetKind.COMMAND,
                    target_value=segment.strip(),
                    action=DangerAction.ELEVATED,
                    suggested_alternatives=("展示修改内容并由用户显式确认",),
                )
            if _shell_has_mutation(segment) and _text_contains_git_component(normalized):
                return DangerRule(
                    name="git-internal",
                    reason="shell 命令写入 .git 内部文件会改变或破坏仓库状态",
                    matcher=".git/",
                    target_kind=DangerTargetKind.COMMAND,
                    target_value=segment.strip(),
                    suggested_alternatives=("使用 git 命令完成仓库状态变更",),
                )
        return None

    def _match_blocked_path(self, request: ApprovalRequest) -> DangerRule | None:
        """消费绝对前缀与 Git 项目相对的 HardBlock 路径规则。"""
        operation = _request_path_operation(request.tool_name)
        if operation is None:
            return None
        raw_path = _request_path(request.arguments)
        if raw_path is None:
            return None
        try:
            target = _resolve_request_path(raw_path, request)
        except (OSError, RuntimeError, ValueError):
            return DangerRule(
                name="path-resolve-failed",
                reason="危险检测无法解析目标路径",
                matcher="path:resolve",
                target_kind=DangerTargetKind.PATH,
                target_value=raw_path,
                suggested_alternatives=("改用明确的绝对路径",),
            )
        for rule, prefix in self._blocked_paths:
            if prefix is not None and operation in rule.ops and _is_under(target, prefix):
                return DangerRule(
                    name=rule.name,
                    reason=rule.reason,
                    matcher=rule.matcher,
                    target_kind=DangerTargetKind.PATH,
                    target_value=str(target),
                    suggested_alternatives=("由用户手工处理该敏感路径",),
                )
        project_rule = _match_project_relative_blocked_path(
            request=request,
            raw_path=raw_path,
            target=target,
            operation=operation,
            rules=self._blocked_paths,
        )
        if project_rule is not None:
            return project_rule
        return None

    def _match_shell_command(self, request: ApprovalRequest) -> DangerRule | None:
        """复用原 HardBlock 与 destructive 的整句及段级命令规则。"""
        if request.tool_name not in _SHELL_TOOLS:
            return None
        command = _request_command(request.arguments)
        if command is None:
            return None
        hard_hit = _match_command_rules(command, self._hard_commands)
        if hard_hit is not None:
            rule, target = hard_hit
            return DangerRule(
                name=rule.name,
                reason=rule.reason,
                matcher=rule.matcher,
                target_kind=DangerTargetKind.COMMAND,
                target_value=target,
                suggested_alternatives=("缩小命令作用范围并再次请求审批",),
            )
        destructive_hit = _match_command_rules(command, self._destructive_commands)
        if destructive_hit is None:
            return None
        rule, target = destructive_hit
        return DangerRule(
            name=rule.name,
            reason=rule.reason,
            matcher=rule.matcher,
            target_kind=DangerTargetKind.COMMAND,
            target_value=target,
            action=DangerAction.FORCE_ASK,
            suggested_alternatives=("确认目标清单后由用户显式批准该不可逆操作",),
        )


def _compile_hard_commands(
    rules: tuple[DangerCommandRule, ...],
) -> tuple[tuple[DangerCommandRule, re.Pattern[str]], ...]:
    """预编译 HardBlock 的 segment_regex 规则。"""
    return tuple(
        (rule, re.compile(rule.matcher)) for rule in rules if rule.match_mode == "segment_regex"
    )


def _compile_destructive_commands(
    rules: tuple[DangerCommandRule, ...],
) -> tuple[tuple[DangerCommandRule, re.Pattern[str]], ...]:
    """预编译 destructive 的 segment_regex 规则。"""
    return tuple(
        (rule, re.compile(rule.matcher)) for rule in rules if rule.match_mode == "segment_regex"
    )


def _resolve_blocked_paths(
    rules: tuple[DangerPathRule, ...],
) -> tuple[tuple[DangerPathRule, Path | None], ...]:
    """解析 HardBlock 路径规则；项目相对规则延迟到请求 cwd 判定。"""
    resolved: list[tuple[DangerPathRule, Path | None]] = []
    for rule in rules:
        if rule.effect != "block":
            continue
        if rule.match_mode == "project_relative":
            resolved.append((rule, None))
            continue
        resolved.append((rule, Path(rule.matcher).expanduser().resolve(strict=False)))
    return tuple(resolved)


def _match_project_relative_blocked_path(
    *,
    request: ApprovalRequest,
    raw_path: str,
    target: Path,
    operation: str,
    rules: tuple[tuple[DangerPathRule, Path | None], ...],
) -> DangerRule | None:
    """以请求 cwd 的 Git 顶层解释项目相对 HardBlock 规则。"""
    project_rules = tuple(rule for rule, prefix in rules if prefix is None)
    if not project_rules:
        return None
    root = _discover_project_root(request, target)
    if root is None:
        if any(_looks_like_project_relative_target(raw_path, rule) for rule in project_rules):
            return DangerRule(
                name="project-root-unavailable",
                reason="无法解析 Git 项目根，保守拒绝访问项目内部受保护目标",
                matcher="project_relative:root",
                target_kind=DangerTargetKind.PATH,
                target_value=str(target),
                suggested_alternatives=("在已识别的 Git 工作目录中通过 git 命令操作仓库",),
            )
        return None
    for rule in project_rules:
        if operation not in rule.ops:
            continue
        protected_root = (root / rule.matcher).resolve(strict=False)
        if _is_under(target, protected_root):
            return DangerRule(
                name=rule.name,
                reason=rule.reason,
                matcher=rule.matcher,
                target_kind=DangerTargetKind.PATH,
                target_value=str(target),
                suggested_alternatives=("使用 git 命令完成仓库状态变更",),
            )
    return None


def _discover_project_root(request: ApprovalRequest, target: Path) -> Path | None:
    """从请求 cwd 优先向上寻找 Git 顶层，目标路径用于缺失 cwd 时回退。"""
    request_context = SafetyRequestContext.from_request(request)
    starts: list[Path] = []
    if request_context.cwd is not None:
        starts.append(Path(request_context.cwd).expanduser().resolve(strict=False))
    starts.append(target if target.is_dir() else target.parent)
    visited: set[Path] = set()
    for start in starts:
        for candidate in (start, *start.parents):
            if candidate in visited:
                continue
            visited.add(candidate)
            marker = candidate / ".git"
            if marker.is_dir() or marker.is_file():
                return candidate
    return None


def _looks_like_project_relative_target(raw_path: str, rule: DangerPathRule) -> bool:
    """在 Git 根不可得时识别显式指向项目保护目录的请求。"""
    matcher_head = Path(rule.matcher).parts[0].casefold()
    return any(part.casefold() == matcher_head for part in Path(raw_path).parts)


def _match_command_rules(
    command: str,
    rules: tuple[
        tuple[DangerCommandRule, re.Pattern[str]],
        ...,
    ],
) -> tuple[DangerCommandRule, str] | None:
    """先匹配整句，再逐段匹配命令规则。"""
    whole = command.strip()
    for rule, pattern in rules:
        if pattern.search(whole):
            return rule, whole
    for segment in _split_shell_segments(command):
        target = segment.strip()
        if not target:
            continue
        for rule, pattern in rules:
            if pattern.search(target):
                return rule, target
    return None


def _request_path(arguments: dict[str, Any]) -> str | None:
    """从 canonical 或 SDK 参数中提取文件路径。"""
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _request_command(arguments: dict[str, Any]) -> str | None:
    """从请求参数中提取非空 shell 命令。"""
    value = arguments.get("command")
    if isinstance(value, str) and value.strip():
        return value
    return None


def _resolve_request_path(raw_path: str, request: ApprovalRequest) -> Path:
    """结合请求 cwd 解析目标路径并消除 ``..``。"""
    target = Path(raw_path).expanduser()
    if target.is_absolute():
        return target.resolve(strict=False)
    base = SafetyRequestContext.from_request(request).path_base()
    return (base / target).resolve(strict=False)


def _match_path_resolution_failure(request: ApprovalRequest) -> DangerRule | None:
    """把写入和敏感读取的非法路径解析失败收敛为危险命中。"""
    if _request_path_operation(request.tool_name) is None:
        return None
    raw_path = _request_path(request.arguments)
    if raw_path is None:
        return None
    try:
        _resolve_request_path(raw_path, request)
    except (OSError, RuntimeError, ValueError):
        return DangerRule(
            name="path-resolve-failed",
            reason="危险检测无法解析目标路径",
            matcher="path:resolve",
            target_kind=DangerTargetKind.PATH,
            target_value=raw_path,
            suggested_alternatives=("改用明确的绝对路径",),
        )
    return None


def _request_path_operation(tool_name: str) -> str | None:
    """把路径工具名映射为敏感规则的 read/write 操作。"""
    if tool_name in _READ_TOOLS:
        return "read"
    if tool_name in _WRITE_TOOLS:
        return "write"
    return None


def _is_under(target: Path, root: Path) -> bool:
    """判断 canonical 目标是否位于 canonical 根目录内。"""
    try:
        return target == root or target.is_relative_to(root)
    except ValueError:
        return False


def _contains_git_component(path: Path) -> bool:
    """判断路径组件中是否包含精确的 ``.git``。"""
    return any(part.casefold() == ".git" for part in path.parts)


def _instruction_write_action(
    request: ApprovalRequest,
    target: Path,
    prompts_root: Path,
) -> DangerAction | None:
    """将指令文件的清空归为 block，其余编辑归为 elevated。"""
    is_instruction = target.name.casefold() in {"agents.md", "claude.md"} or _is_under(
        target, prompts_root
    )
    if not is_instruction:
        return None
    if request.tool_name == "write_file":
        content = request.arguments.get("content")
        return (
            DangerAction.BLOCK
            if not isinstance(content, str) or not content.strip()
            else DangerAction.ELEVATED
        )
    old_string = request.arguments.get("old_string")
    new_string = request.arguments.get("new_string")
    if isinstance(old_string, str) and isinstance(new_string, str) and not new_string:
        try:
            return (
                DangerAction.BLOCK
                if old_string == target.read_text(encoding="utf-8")
                else DangerAction.ELEVATED
            )
        except (OSError, UnicodeError):
            return DangerAction.ELEVATED
    return DangerAction.ELEVATED


def _is_self_configuration_path(target: Path) -> bool:
    """识别完全信任下仍需人工确认的自我配置目标。"""
    return target.name.casefold().startswith(".env") or any(
        part.casefold() == ".kongming" for part in target.parts
    )


def _shell_destroys_instruction_file(command: str) -> bool:
    """识别 rm、mv 和重定向清空 AGENTS.md / CLAUDE.md 的常见形态。"""
    instruction = r"(?:\S*/)?(?:AGENTS|CLAUDE)\.md(?=\s|$)"
    return (
        re.search(rf"^\s*(?:rm|mv)\b.*{instruction}", command, re.IGNORECASE) is not None
        or re.search(rf">\s*{instruction}", command, re.IGNORECASE) is not None
    )


def _shell_destroys_git_directory(command: str) -> bool:
    """识别 rm/mv 整个 .git 目录的项目完整性破坏操作。"""
    return (
        re.search(
            r"^\s*(?:rm|mv)\b.*(?<!\S)(?:\S*/)?\.git(?:/[^\s]*)?(?=\s|$)",
            command,
            re.IGNORECASE,
        )
        is not None
    )


def _shell_mutates_instruction_file(command: str) -> bool:
    """识别保留内容的 shell 指令文件编辑，交给 elevated 人审。"""
    instruction = r"(?:\S*/)?(?:AGENTS|CLAUDE)\.md(?=\s|$)"
    return (
        _shell_has_mutation(command)
        and re.search(
            instruction,
            command,
            re.IGNORECASE,
        )
        is not None
    )


def _shell_mutates_self_configuration(command: str) -> bool:
    """识别 shell 对 .env 或 .kongming 的写入，固定进入 elevated 人审。"""
    return _shell_has_mutation(command) and (
        re.search(r"(?:^|[/\s])\.env[^/\s]*(?:$|[/\s])", command, re.IGNORECASE) is not None
        or re.search(r"(?:^|[/\s])\.kongming(?:$|[/\s])", command, re.IGNORECASE) is not None
    )


def _text_contains_git_component(value: str) -> bool:
    """判断 shell 文本中是否出现精确 ``.git`` 路径组件。"""
    normalized = "/" + value.replace("\\", "/").strip("/")
    return (
        re.search(r"(?:^|[/\s(=,])\.git(?:/|$|\s|[\"')])", normalized, flags=re.IGNORECASE)
        is not None
    )


def _split_shell_segments(command: str) -> tuple[str, ...]:
    """去掉引号转义字符后按 shell 连接符拆分命令。"""
    cleaned = command.replace('"', " ").replace("'", " ").replace("\\", "/")
    return tuple(_SHELL_SEGMENT_SPLITTER.split(cleaned))


def _normalize_shell_text(value: str) -> str:
    """统一 shell 路径分隔符和引号，供路径标记检查。"""
    return value.replace("\\", "/").replace('"', "").replace("'", "")


def _shell_has_mutation(command: str) -> bool:
    """保守识别含文件写入语义的 shell 命令。"""
    if ">" in command:
        return True
    if _python_write_targets(command):
        return True
    if (
        _PYTHON_FILE_WRITE_RE.search(command)
        or _PYTHON_PATH_WRITE_RE.search(command)
        or _POWERSHELL_MUTATION_RE.search(command)
        or _DD_FILE_WRITE_RE.search(command)
    ):
        return True
    for segment in _split_shell_segments(command):
        tokens = segment.strip().split()
        if not tokens:
            continue
        executable = Path(tokens[0]).name.casefold()
        if executable in _SHELL_MUTATION_COMMANDS:
            return True
        if executable == "sed" and any(token == "-i" or token.startswith("-i") for token in tokens):
            return True
    return False


def _content_fragments(arguments: dict[str, Any]) -> tuple[str, ...]:
    """提取 write/edit 请求中表达新状态的文本片段。"""
    return tuple(
        value
        for key in _CONTENT_KEYS
        if isinstance((value := arguments.get(key)), str) and value.strip()
    )


def _expands_thread_allow(arguments: dict[str, Any], *, target: Path) -> bool:
    """检测 thread permissions 新内容是否增加 allow 表达式。"""
    old_allow = _load_existing_allow(target)
    if arguments.get("old_string") is not None or arguments.get("new_string") is not None:
        return _edit_expands_existing_allow(arguments, target=target, old_allow=old_allow)
    fragments = _content_fragments(arguments)
    for fragment in fragments:
        parsed_allow = _parse_allow_entries(fragment)
        if parsed_allow is not None:
            if parsed_allow - old_allow:
                return True
            continue
        match = _ALLOW_LIST_RE.search(fragment)
        if match is not None and match.group("body").strip():
            return True
    return _edit_expands_existing_allow(arguments, target=target, old_allow=old_allow)


def _edit_expands_existing_allow(
    arguments: dict[str, Any],
    *,
    target: Path,
    old_allow: frozenset[str],
) -> bool:
    """在现有本子文本上模拟 edit_file 替换并比较 allow 集合。"""
    edit = _simulate_text_edit(arguments, target=target)
    if edit is None:
        return False
    _, updated_text = edit
    updated_allow = _parse_allow_entries(updated_text)
    return updated_allow is not None and bool(updated_allow - old_allow)


def _simulate_text_edit(
    arguments: dict[str, Any],
    *,
    target: Path,
) -> tuple[str, str] | None:
    """读取目标文件并模拟一次 old_string 到 new_string 的精确替换。"""
    old_string = arguments.get("old_string")
    new_string = arguments.get("new_string")
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return None
    if not old_string:
        return None
    try:
        current = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if old_string not in current:
        return None
    replace_all = arguments.get("replace_all") is True
    updated = (
        current.replace(old_string, new_string)
        if replace_all
        else current.replace(
            old_string,
            new_string,
            1,
        )
    )
    return current, updated


def _python_write_targets(command: str) -> tuple[str, ...]:
    """解析 shell 中 ``python -c`` 代码并返回明确的文件写入目标。"""
    try:
        tokens = tuple(shlex.split(command, posix=True))
    except ValueError:
        return ()
    targets: list[str] = []
    for index, token in enumerate(tokens):
        if not _is_python_executable(token):
            continue
        code_index = _python_code_token_index(tokens, executable_index=index)
        if code_index is None:
            continue
        if code_index + 1 >= len(tokens):
            continue
        targets.extend(_python_code_write_targets(tokens[code_index + 1]))
    return tuple(dict.fromkeys(targets))


def _python_code_token_index(
    tokens: tuple[str, ...],
    *,
    executable_index: int,
) -> int | None:
    """在当前 Python shell 段内定位 ``-c``，避免跨命令误关联。"""
    for index in range(executable_index + 1, len(tokens)):
        token = tokens[index]
        if token in {"&&", "||", ";", "|"}:
            return None
        if token == "-c":
            return index
    return None


def _is_python_executable(token: str) -> bool:
    """判断 shell token 是否为 py/python 系列解释器。"""
    executable = Path(token).name.casefold()
    return re.fullmatch(r"(?:py|python(?:3(?:\.\d+)*)?)(?:\.exe)?", executable) is not None


def _python_code_write_targets(code: str) -> tuple[str, ...]:
    """通过 AST 识别 open 写模式与 pathlib 写方法的常量路径。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ()
    path_bindings = _python_path_bindings(tree)
    targets: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        open_target = _python_open_write_target(node)
        if open_target is not None:
            targets.append(open_target)
        pathlib_target = _python_pathlib_write_target(node, bindings=path_bindings)
        if pathlib_target is not None:
            targets.append(pathlib_target)
    return tuple(dict.fromkeys(targets))


def _python_path_bindings(tree: ast.AST) -> dict[str, str]:
    """收集 ``变量 = Path('常量路径')`` 形式的 pathlib 绑定。"""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            path = _python_path_constructor_target(node.value)
            if path is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = path
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            path = _python_path_constructor_target(node.value)
            if path is not None and isinstance(node.target, ast.Name):
                bindings[node.target.id] = path
    return bindings


def _python_open_write_target(call: ast.Call) -> str | None:
    """识别 builtin open 的位置或关键字写模式并返回常量 file 路径。"""
    if not _python_callable_named(call.func, "open"):
        return None
    path_node = call.args[0] if call.args else _python_keyword_value(call, "file")
    mode_node = call.args[1] if len(call.args) > 1 else _python_keyword_value(call, "mode")
    path = _python_literal_string(path_node)
    mode = _python_literal_string(mode_node)
    if path is None or mode is None or not any(marker in mode for marker in "wax+"):
        return None
    return path


def _python_pathlib_write_target(
    call: ast.Call,
    *,
    bindings: dict[str, str],
) -> str | None:
    """识别直接或变量绑定的 Path.write_text/write_bytes/Path.open 写入。"""
    if not isinstance(call.func, ast.Attribute):
        return None
    method = call.func.attr
    owner = call.func.value
    target = (
        bindings.get(owner.id)
        if isinstance(owner, ast.Name)
        else _python_path_constructor_target(owner)
    )
    if target is None:
        return None
    if method in {"write_bytes", "write_text"}:
        return target
    if method != "open":
        return None
    mode_node = call.args[0] if call.args else _python_keyword_value(call, "mode")
    mode = _python_literal_string(mode_node)
    return target if mode is not None and any(marker in mode for marker in "wax+") else None


def _python_path_constructor_target(node: ast.AST) -> str | None:
    """从 Path/pathlib.Path 常量构造调用提取路径。"""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    if not _python_callable_named(node.func, "Path"):
        return None
    return _python_literal_string(node.args[0])


def _python_callable_named(node: ast.AST, expected: str) -> bool:
    """判断调用目标是否为裸名称或属性形式的指定函数。"""
    if isinstance(node, ast.Name):
        return node.id == expected
    return isinstance(node, ast.Attribute) and node.attr == expected


def _python_keyword_value(call: ast.Call, name: str) -> ast.AST | None:
    """从 AST 调用中读取指定关键字参数值。"""
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _python_literal_string(node: ast.AST | None) -> str | None:
    """读取 AST 常量字符串；动态表达式返回 ``None``。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _load_existing_allow(target: Path) -> frozenset[str]:
    """读取现有本子的 allow 集合；缺失或损坏文件按空集合处理。"""
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(payload, dict):
        return frozenset()
    allow = payload.get("allow")
    if not isinstance(allow, list) or not all(isinstance(item, str) for item in allow):
        return frozenset()
    return frozenset(allow)


def _parse_allow_entries(fragment: str) -> frozenset[str] | None:
    """从完整 JSON/YAML 内容中提取 allow 集合。"""
    try:
        payload = yaml.safe_load(fragment)
    except yaml.YAMLError:
        return None
    if not isinstance(payload, dict) or "allow" not in payload:
        return None
    allow = payload.get("allow")
    if not isinstance(allow, list) or not all(isinstance(item, str) for item in allow):
        return None
    return frozenset(allow)


def _expands_legacy_global_allow_for_request(
    request: ApprovalRequest,
    *,
    target: Path,
) -> bool:
    """按 write 全文或 edit 模拟结果检测旧根级 allow 自提权。"""
    if request.tool_name == "edit_file":
        edit = _simulate_text_edit(request.arguments, target=target)
        if edit is None:
            return False
        before_text, after_text = edit
        before = _root_wide_allow_expressions(_parse_legacy_config_payload(before_text))
        after = _root_wide_allow_expressions(_parse_legacy_config_payload(after_text))
        return bool(after - before)
    for fragment in _content_fragments(request.arguments):
        payload = _parse_legacy_config_payload(fragment)
        if _root_wide_allow_expressions(payload):
            return True
    return False


def _root_wide_allow_expressions(payload: object) -> frozenset[str]:
    """提取旧配置中会扩大宿主写权限的 allow 表达式集合。"""
    if not isinstance(payload, dict):
        return frozenset()
    safety = payload.get("safety")
    if not isinstance(safety, dict):
        return frozenset()
    rules = safety.get("approval_rules")
    if not isinstance(rules, list):
        return frozenset()
    expressions: set[str] = set()
    for item in rules:
        if not isinstance(item, dict) or item.get("behavior") != "allow":
            continue
        expression = item.get("expression")
        if isinstance(expression, str) and _is_root_wide_allow_expression(expression):
            expressions.add(expression)
    return frozenset(expressions)


def _parse_legacy_config_payload(fragment: str) -> object:
    """按 YAML/JSON 后 TOML 的顺序解析旧配置内容。"""
    try:
        payload = yaml.safe_load(fragment)
    except yaml.YAMLError:
        payload = None
    if isinstance(payload, dict):
        return payload
    try:
        return tomllib.loads(fragment)
    except tomllib.TOMLDecodeError:
        return None


def _is_legacy_kongming_config_target(target: Path) -> bool:
    """判断目标是否为项目 ``.kongming/config`` 的四种旧格式。"""
    valid_name = target.name.casefold() in {
        "config.json",
        "config.toml",
        "config.yaml",
        "config.yml",
    }
    return valid_name and any(part.casefold() == ".kongming" for part in target.parts)


def _shell_targets_legacy_kongming_config(command: str) -> bool:
    """判断 shell 文本是否显式指向旧项目安全配置。"""
    normalized = command.replace("\\", "/").casefold()
    return any(
        marker in normalized
        for marker in (
            ".kongming/config.json",
            ".kongming/config.toml",
            ".kongming/config.yaml",
            ".kongming/config.yml",
        )
    )


def _is_root_wide_allow_expression(expression: str) -> bool:
    """判断旧 canonical allow 是否授予根级宿主能力。"""
    try:
        match = parse_rule_expression(expression, cwd=None)
    except ValueError:
        return False
    if match.kind in {MatcherKind.PATH_PREFIX, MatcherKind.PATH_GLOB}:
        home = Path.home().resolve(strict=False).as_posix()
        return (
            match.pattern == "/"
            or match.pattern == home
            or any(
                match.pattern == prefix or match.pattern.startswith(prefix + "/")
                for prefix in _ROOT_LIKE_PATH_PREFIXES
                if prefix.startswith("/") and prefix != "/"
            )
        )
    if match.kind is MatcherKind.TOOL_EXACT:
        return match.tool_name in {"run_shell", "write_file", "edit_file"}
    return match.kind is MatcherKind.TOOL_GLOB and match.tool_name.startswith(("write_", "edit_"))


def _policy_rule(
    *,
    reason: str,
    matcher: str,
    target: Path,
    target_value: str | None = None,
) -> DangerRule:
    """构造保留人工确认的安全配置写入命中。"""
    return DangerRule(
        name="safety-policy-self-escalation",
        reason=reason,
        matcher=matcher,
        target_kind=DangerTargetKind.SAFETY_POLICY,
        target_value=target_value or str(target),
        action=DangerAction.ELEVATED,
        suggested_alternatives=("通过审批设置页或 thread permissions 门户提交修改",),
    )


__all__ = ["DangerAction", "DangerGuard", "DangerRule", "DangerTargetKind"]
