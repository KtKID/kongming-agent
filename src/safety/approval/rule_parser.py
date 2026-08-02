"""统一审批规则 DSL 的解析、canonicalize 与匹配。"""

from __future__ import annotations

import fnmatch
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from core.contracts import ApprovalRequest
from safety.approval.rule_models import MatcherKind, RuleMatch

_EXPRESSION_RE = re.compile(r"^(?P<tool>[a-zA-Z_][a-zA-Z0-9_*]*)(?:\((?P<pattern>.*)\))?$")
_MCP_RE = re.compile(r"^mcp__[a-zA-Z0-9_-]+__[a-zA-Z0-9_*-]+$")
_PATH_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "view_file",
    }
)
_PATH_KEYS = ("path", "file_path")
_UNSAFE_SHELL_MARKERS = ("&&", "||", ";", "|", "&", "\n", "\r", "`", "$", "<", ">")


def parse_rule_expression(expression: str, *, cwd: str | None = None) -> RuleMatch:
    """把 DSL 文本解析为唯一 canonical matcher。"""
    if not expression or expression != expression.strip():
        raise ValueError("rule expression must be non-empty canonical text")
    match = _EXPRESSION_RE.fullmatch(expression)
    if match is None:
        raise ValueError(f"invalid rule expression: {expression!r}")

    tool_name = match.group("tool")
    pattern = match.group("pattern")
    if tool_name == "*" or tool_name.startswith("*"):
        raise ValueError("leading or global tool wildcard is forbidden")

    if pattern is None:
        return _parse_tool_expression(tool_name)
    if tool_name == "run_shell":
        return _parse_shell_expression(pattern)
    if tool_name in _PATH_TOOLS:
        return _parse_path_expression(tool_name, pattern, cwd=cwd)
    raise ValueError(f"tool {tool_name!r} does not support an argument matcher")


def matches_rule(rule_match: RuleMatch, request: ApprovalRequest, *, cwd: str) -> bool:
    """按 token、路径组件或 canonical 工具名边界匹配请求。"""
    kind = rule_match.kind
    if kind in (MatcherKind.TOOL_EXACT, MatcherKind.MCP_EXACT):
        return request.tool_name == rule_match.tool_name
    if kind in (MatcherKind.TOOL_GLOB, MatcherKind.MCP_GLOB):
        return fnmatch.fnmatchcase(request.tool_name, rule_match.tool_name)
    if request.tool_name != rule_match.tool_name:
        return False
    if kind is MatcherKind.SHELL_PREFIX:
        command = request.arguments.get("command")
        return isinstance(command, str) and _shell_prefix_matches(command, rule_match.pattern)
    if kind in (MatcherKind.PATH_PREFIX, MatcherKind.PATH_GLOB):
        raw_path = _request_path(request.arguments)
        if raw_path is None:
            return False
        candidate = _canonical_request_path(raw_path, cwd=cwd)
        if kind is MatcherKind.PATH_PREFIX:
            return candidate == rule_match.pattern or candidate.startswith(rule_match.pattern + "/")
        return PurePosixPath(candidate).match(rule_match.pattern)
    return False


def canonical_cwd(cwd: str) -> str:
    """返回绝对 POSIX cwd，供 scope 和 path matcher 共用。"""
    if not cwd or not cwd.strip():
        raise ValueError("cwd must be non-empty")
    path = Path(cwd).expanduser()
    if not path.is_absolute():
        raise ValueError("cwd must be absolute")
    return path.resolve(strict=False).as_posix()


def canonical_tool_name(tool_name: str) -> str:
    """校验并返回 canonical 工具名。"""
    if not tool_name or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", tool_name):
        raise ValueError(f"invalid tool name: {tool_name!r}")
    return tool_name


def shell_prefix_tokens(command: str, *, max_tokens: int | None = None) -> tuple[str, ...]:
    """解析 shell token；动态命令、链式命令和空命令返回空元组。"""
    if not command or not command.strip():
        return ()
    try:
        tokens = tuple(shlex.split(command, posix=True))
    except ValueError:
        return ()
    if not tokens or tokens[0] in {"&&", "||", ";", "|"}:
        return ()
    if any(marker in command for marker in _UNSAFE_SHELL_MARKERS):
        return ()
    if "=" in tokens[0]:
        return ()
    if max_tokens is not None:
        return tokens[:max_tokens]
    return tokens


def _parse_tool_expression(tool_name: str) -> RuleMatch:
    if "*" in tool_name:
        if tool_name.count("*") != 1 or not tool_name.endswith("*"):
            raise ValueError("tool wildcard must be a single suffix wildcard")
        if tool_name.startswith("mcp__"):
            if not _MCP_RE.fullmatch(tool_name) or tool_name == "mcp__*__*":
                raise ValueError("invalid MCP wildcard")
            kind = MatcherKind.MCP_GLOB
        else:
            if len(tool_name.removesuffix("*")) < 2:
                raise ValueError("tool wildcard prefix is too broad")
            kind = MatcherKind.TOOL_GLOB
    elif tool_name.startswith("mcp__"):
        if not _MCP_RE.fullmatch(tool_name):
            raise ValueError("invalid MCP tool name")
        kind = MatcherKind.MCP_EXACT
    else:
        canonical_tool_name(tool_name)
        kind = MatcherKind.TOOL_EXACT
    return RuleMatch(
        kind=kind,
        tool_name=tool_name,
        pattern="",
        canonical_expression=tool_name,
    )


def _parse_shell_expression(pattern: str) -> RuleMatch:
    if not pattern.endswith(":*"):
        raise ValueError("shell matcher must end with ':*'")
    prefix = pattern[:-2].strip()
    if not prefix or "*" in prefix:
        raise ValueError("shell prefix must be concrete")
    tokens = shell_prefix_tokens(prefix)
    if not tokens:
        raise ValueError("shell prefix must contain safe concrete tokens")
    canonical_prefix = shlex.join(tokens)
    return RuleMatch(
        kind=MatcherKind.SHELL_PREFIX,
        tool_name="run_shell",
        pattern=canonical_prefix,
        canonical_expression=f"run_shell({canonical_prefix}:*)",
    )


def _parse_path_expression(tool_name: str, pattern: str, *, cwd: str | None) -> RuleMatch:
    if not pattern or pattern != pattern.strip():
        raise ValueError("path matcher must be canonical non-empty text")
    expanded = Path(pattern).expanduser() if "*" not in pattern else None
    if expanded is not None and not expanded.is_absolute():
        raise ValueError("path rule expression must use an absolute path")
    if "*" in pattern:
        if not Path(pattern).is_absolute():
            raise ValueError("path glob must be absolute")
        if pattern.endswith("/**") and pattern.count("*") == 2:
            raw_prefix = pattern[:-3] or "/"
            prefix = Path(raw_prefix).resolve(strict=False).as_posix()
            canonical_pattern = f"{prefix.rstrip('/')}/**"
            return RuleMatch(
                kind=MatcherKind.PATH_PREFIX,
                tool_name=tool_name,
                pattern=prefix,
                canonical_expression=f"{tool_name}({canonical_pattern})",
            )
        canonical_pattern = PurePosixPath(pattern).as_posix()
        return RuleMatch(
            kind=MatcherKind.PATH_GLOB,
            tool_name=tool_name,
            pattern=canonical_pattern,
            canonical_expression=f"{tool_name}({canonical_pattern})",
        )
    if expanded is None:
        raise ValueError("path matcher could not be canonicalized")
    canonical_path = expanded.resolve(strict=False).as_posix()
    return RuleMatch(
        kind=MatcherKind.PATH_PREFIX,
        tool_name=tool_name,
        pattern=canonical_path,
        canonical_expression=f"{tool_name}({canonical_path}/**)",
    )


def _request_path(arguments: dict[str, Any]) -> str | None:
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _canonical_request_path(raw_path: str, *, cwd: str) -> str:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path(canonical_cwd(cwd)) / path
    return path.resolve(strict=False).as_posix()


def _shell_prefix_matches(command: str, prefix: str) -> bool:
    command_tokens = shell_prefix_tokens(command)
    prefix_tokens = shell_prefix_tokens(prefix)
    if not command_tokens or not prefix_tokens or len(command_tokens) < len(prefix_tokens):
        return False
    return command_tokens[: len(prefix_tokens)] == prefix_tokens


__all__ = [
    "canonical_cwd",
    "canonical_tool_name",
    "matches_rule",
    "parse_rule_expression",
    "shell_prefix_tokens",
]
