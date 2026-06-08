"""规则 matcher 纯函数模块（零外部依赖）。

本模块只接受 ``MatchSpec``（dict） + ``tool_name`` + ``tool_input`` → ``bool``，
不读 yaml / 不写文件 / 不感知 policy。可独立单测。

核心函数::

    normalize_bash_cmd(cmd) -> list[str]   # 拆 wrapper（rtk/sudo/env/PATH=）
    split_chained(cmd)     -> list[str]    # 拆 | && ; 分隔的子命令
    matches(spec, tool_name, tool_input) -> bool   # 入口：分派到具体 matcher

设计要点：
- ``normalize_bash_cmd`` 用 ``shlex`` 拆 token，去掉常见 wrapper 前缀（按白名单）
- ``split_chained`` 简单 split 三种分隔符，**不解析嵌套 quote**（嵌套
  shell 由 ``bash_nested_shell`` 规则单独拦截，无须 matcher 智能化）
- ``matches`` 是 **OR 语义**：任一子命令命中 → 整条命令命中
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from typing import Any

# ---------------------------------------------------------------------------
# Bash 命令归一化与拆分
# ---------------------------------------------------------------------------

# wrapper 前缀白名单——出现在命令首 token 时剥离
# 仅剥离"工具型 wrapper"，不剥 sudo（sudo 本身要拦）
_BASH_WRAPPER_PREFIXES: frozenset[str] = frozenset(
    [
        "rtk",  # 项目内 token-killer
        "env",  # env VAR=val cmd
        "/usr/bin/env",
        "command",  # command cmd
        "exec",  # exec cmd
        "nice",
        "/usr/bin/nice",
        "ionice",
        "time",
        "/usr/bin/time",
        "stdbuf",
    ],
)

# `env -u VAR cmd` / `env VAR=val cmd` 这两种 env 拼接的额外参数模式
_ENV_FLAG_RE = re.compile(r"^-[uiSI]$")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def normalize_bash_cmd(cmd: str) -> list[str]:
    """剥离 wrapper 前缀，返回主命令 token 列表。

    例：

    - ``"rtk rm -rf /tmp"`` → ``["rm", "-rf", "/tmp"]``
    - ``"env -u RTK_HOOK rm x"`` → ``["rm", "x"]``
    - ``"PATH=/foo:$PATH npm test"`` → ``["npm", "test"]``
    - ``"sudo rm -rf /"`` → ``["sudo", "rm", "-rf", "/"]``  # sudo 不剥
    - ``"rm -rf x"`` → ``["rm", "-rf", "x"]``  # 无 wrapper

    Args:
        cmd: 单条 shell 命令字符串（不应含 ``|`` / ``&&`` / ``;``）

    Returns:
        归一化后的 token 列表；空列表表示无法解析。
    """
    if not cmd or not cmd.strip():
        return []

    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        # shlex 解析失败（未闭合的引号）→ 退化为空格 split
        tokens = cmd.split()

    if not tokens:
        return []

    # 第一步：剥前缀环境变量赋值 `VAR=val`
    while tokens and _ENV_ASSIGN_RE.match(tokens[0]):
        tokens.pop(0)

    if not tokens:
        return []

    # 第二步：迭代剥 wrapper（可能嵌套 `rtk env -u X rm`）
    safety_iters = 8
    while tokens and safety_iters > 0:
        safety_iters -= 1
        head = tokens[0]
        if head not in _BASH_WRAPPER_PREFIXES:
            break

        # 是 wrapper：剥头部
        if head in ("env", "/usr/bin/env"):
            tokens.pop(0)
            # 继续剥 env 的 flag/assign 参数
            while tokens and (_ENV_FLAG_RE.match(tokens[0]) or _ENV_ASSIGN_RE.match(tokens[0])):
                t = tokens.pop(0)
                # `-u VAR` 还要再吞一个 VAR
                if t in ("-u", "-S", "-I") and tokens:
                    tokens.pop(0)
            continue

        if head in ("nice", "/usr/bin/nice"):
            tokens.pop(0)
            # `nice -n 10 cmd` 或 `nice cmd`；吞掉 -n N
            while tokens and tokens[0].startswith("-"):
                t = tokens.pop(0)
                if t == "-n" and tokens:
                    tokens.pop(0)
            continue

        if head in ("ionice",):
            tokens.pop(0)
            while tokens and tokens[0].startswith("-"):
                t = tokens.pop(0)
                if t in ("-c", "-n", "-p") and tokens:
                    tokens.pop(0)
            continue

        if head in ("time", "/usr/bin/time"):
            tokens.pop(0)
            # `time -p cmd` 或 `time cmd`
            while tokens and tokens[0].startswith("-"):
                tokens.pop(0)
            continue

        if head == "stdbuf":
            tokens.pop(0)
            while tokens and tokens[0].startswith("-"):
                t = tokens.pop(0)
                if t in ("-i", "-o", "-e") and tokens:
                    tokens.pop(0)
            continue

        if head in ("rtk", "command", "exec"):
            tokens.pop(0)
            continue

    return tokens


# split_chained：拆 | && ; （ || 也算）
# 不解析嵌套 quote——这是 v1 的简化（嵌套 shell 由 bash_nested_shell 兜底拦截）
_CHAIN_SPLIT_RE = re.compile(r"\s*(?:\|\||\||&&|;)\s*")


def split_chained(cmd: str) -> list[str]:
    """拆分管道/链式 shell 命令为子命令列表。

    例：

    - ``"cd foo && rm -rf bar"`` → ``["cd foo", "rm -rf bar"]``
    - ``"curl x | bash"`` → ``["curl x", "bash"]``
    - ``"rm a; rm b; rm c"`` → ``["rm a", "rm b", "rm c"]``
    - ``"single_cmd"`` → ``["single_cmd"]``

    Args:
        cmd: 完整 shell 命令字符串

    Returns:
        非空子命令列表（去 trim、去空字符串）
    """
    if not cmd:
        return []
    parts = _CHAIN_SPLIT_RE.split(cmd.strip())
    return [p.strip() for p in parts if p and p.strip()]


# ---------------------------------------------------------------------------
# 具体 matcher 实现（按 MatchSpec.kind 分派）
# ---------------------------------------------------------------------------


def _extract_bash_cmd(tool_input: dict[str, Any] | Any) -> str:
    """从 tool_input 提取 bash 命令字符串。

    Claude SDK 的 ``Bash`` tool 的 input 形如 ``{"command": "rm -rf /tmp"}``。
    """
    if isinstance(tool_input, str):
        return tool_input.strip()
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command", "")
        return str(cmd).strip() if cmd else ""
    return ""


def _extract_edit_path(tool_input: dict[str, Any] | Any) -> str:
    """从 tool_input 提取 Edit/Write/NotebookEdit 的文件路径。

    Claude SDK 的几个写文件 tool 都用 ``file_path`` 字段：
    - ``Edit``: {"file_path": "...", "old_string": "...", "new_string": "..."}
    - ``Write``: {"file_path": "...", "content": "..."}
    - ``NotebookEdit``: {"notebook_path": "...", ...}
    """
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "notebook_path", "path"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _expand_user_path(path: str) -> str:
    """展开 ``~`` 为绝对 home 目录。"""
    if not path:
        return ""
    return os.path.expanduser(path)


def _match_bash_cmd_first(spec: dict[str, Any], tokens: list[str]) -> bool:
    expected = str(spec.get("command", ""))
    if not tokens or not expected:
        return False
    first = tokens[0]
    # 兼容绝对路径形式 `/bin/rm` vs `rm`
    basename = first.rsplit("/", 1)[-1]
    return first == expected or basename == expected


def _match_bash_cmd_first_prefix(spec: dict[str, Any], tokens: list[str]) -> bool:
    prefix = spec.get("prefix", "")
    if not tokens or not prefix:
        return False
    first = tokens[0]
    basename = first.rsplit("/", 1)[-1]
    return first.startswith(prefix) or basename.startswith(prefix)


def _match_bash_cmd_first_in(spec: dict[str, Any], tokens: list[str]) -> bool:
    commands = spec.get("commands", [])
    if not tokens or not isinstance(commands, list):
        return False
    first = tokens[0]
    basename = first.rsplit("/", 1)[-1]
    return first in commands or basename in commands


def _match_bash_cmd_pattern(spec: dict[str, Any], cmd_str: str) -> bool:
    pattern = spec.get("pattern", "")
    if not pattern or not cmd_str:
        return False
    try:
        return re.search(pattern, cmd_str) is not None
    except re.error:
        # 配置文件正则坏 → 保守视为不匹配（matcher 层不抛；rules 加载时已 validate）
        return False


def _match_edit_path_glob(spec: dict[str, Any], path: str) -> bool:
    globs = spec.get("globs", [])
    if not path or not isinstance(globs, list):
        return False
    expanded = _expand_user_path(path)
    for pattern in globs:
        if not isinstance(pattern, str):
            continue
        pat_expanded = _expand_user_path(pattern)
        if fnmatch.fnmatch(expanded, pat_expanded) or fnmatch.fnmatch(path, pattern):
            return True
        # 末段匹配兜底
        if expanded.endswith("/" + pattern.lstrip("*/")):
            return True
    return False


def _match_edit_path_pattern(spec: dict[str, Any], path: str) -> bool:
    pattern = spec.get("pattern", "")
    if not pattern or not path:
        return False
    expanded = _expand_user_path(path)
    try:
        return re.search(pattern, expanded) is not None
    except re.error:
        return False


def _match_mcp_not_in_allowlist(spec: dict[str, Any], tool_name: str) -> bool:
    if not tool_name.startswith("mcp__"):
        return False
    allowlist = spec.get("allowlist", [])
    if not isinstance(allowlist, list):
        return True
    return bool(tool_name not in allowlist)


# ---------------------------------------------------------------------------
# 入口分派
# ---------------------------------------------------------------------------


# Bash 工具的 tool_name（claude SDK 内置）
_BASH_TOOL_NAMES: frozenset[str] = frozenset(["Bash"])

# Edit 系列工具
_EDIT_TOOL_NAMES: frozenset[str] = frozenset(
    ["Edit", "Write", "NotebookEdit", "MultiEdit"],
)


def matches(
    spec: dict[str, Any],
    tool_name: str,
    tool_input: dict[str, Any] | Any,
) -> bool:
    """判定单条 MatchSpec 是否命中 (tool_name, tool_input)。

    Args:
        spec: ``RuleDefinition.match`` 的 dict 形式
        tool_name: SDK 给的工具名（如 ``"Bash"`` / ``"Edit"`` / ``"mcp__foo__bar"``）
        tool_input: SDK 给的工具入参（dict）

    Returns:
        命中返回 ``True``。
    """
    kind = spec.get("kind", "")

    # ---- Bash 类 ----
    if kind in ("bash_cmd_first", "bash_cmd_first_prefix", "bash_cmd_first_in", "bash_cmd_pattern"):
        if tool_name not in _BASH_TOOL_NAMES:
            return False
        cmd_str = _extract_bash_cmd(tool_input)
        if not cmd_str:
            return False

        # bash_cmd_pattern：对**整条**命令做正则匹配（不预拆 split_chained）
        # 原因：`curl x | sh` 这类规则的语义就是"管道串联"，依赖完整命令文本。
        # 正则本身是宽松匹配，单条 `rm -rf x` 或 `cd /tmp && rm -rf x` 都能命中
        # `\brm\s+-[rRf]+` 这种 pattern；wrapper（rtk/env）也不影响 pattern 匹配。
        if kind == "bash_cmd_pattern":
            return _match_bash_cmd_pattern(spec, cmd_str)

        # bash_cmd_first* 系列：拆链式 + normalize wrapper，任一子命令命中即整条命中
        sub_cmds = split_chained(cmd_str)
        if not sub_cmds:
            sub_cmds = [cmd_str]

        for sub in sub_cmds:
            tokens = normalize_bash_cmd(sub)
            if not tokens:
                continue
            if kind == "bash_cmd_first" and _match_bash_cmd_first(spec, tokens):
                return True
            if kind == "bash_cmd_first_prefix" and _match_bash_cmd_first_prefix(spec, tokens):
                return True
            if kind == "bash_cmd_first_in" and _match_bash_cmd_first_in(spec, tokens):
                return True
        return False

    # ---- Edit 类 ----
    if kind in ("edit_path_glob", "edit_path_pattern"):
        if tool_name not in _EDIT_TOOL_NAMES:
            return False
        path = _extract_edit_path(tool_input)
        if not path:
            return False
        if kind == "edit_path_glob":
            return _match_edit_path_glob(spec, path)
        return _match_edit_path_pattern(spec, path)

    # ---- MCP 类 ----
    if kind == "mcp_not_in_allowlist":
        return _match_mcp_not_in_allowlist(spec, tool_name)

    # 未知 kind：保守不匹配（rules.py 加载阶段已 validate）
    return False


__all__ = [
    "matches",
    "normalize_bash_cmd",
    "split_chained",
]
