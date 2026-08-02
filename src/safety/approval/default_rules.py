"""DangerGuard 使用的写死危险规则数据。

本文件只保存不可由配置覆盖的危险命令和凭据路径。普通 allow/deny 规则归属
thread permissions，旧 Boundary、Trust、Grant 与 builtin ask 数据均已退出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class DangerCommandRule:
    """一条按 shell segment 正则匹配的危险命令。"""

    name: str
    matcher: str
    reason: str
    match_mode: Literal["segment_regex"] = "segment_regex"


@dataclass(frozen=True)
class DangerPathRule:
    """一条按绝对路径或 Git 项目相对路径匹配的危险规则。"""

    name: str
    matcher: str
    ops: frozenset[Literal["read", "write", "exec"]]
    reason: str
    match_mode: Literal["path_prefix", "project_relative"] = "path_prefix"
    effect: Literal["block"] = "block"


DEFAULT_HARD_DENY_COMMANDS: tuple[DangerCommandRule, ...] = (
    DangerCommandRule(
        name="host-root-delete",
        matcher=(
            r"^rm\s+(-[a-zA-Z]*r[a-zA-Z]*|-r|--recursive)\b"
            r"(\s+--no-preserve-root)?\s+(/|~|\$HOME|\.|/\*)\s*$"
        ),
        reason="删除宿主根目录或主目录",
    ),
    DangerCommandRule(
        name="rm-rf-no-preserve-root",
        matcher=r"(?<!\S)--no-preserve-root(?!\S)",
        reason="禁用根目录保护",
    ),
    DangerCommandRule(
        name="raw-disk-write",
        matcher=r"\bdd\s+.*of=/dev/(disk|sd|nvme|hd|mmc)",
        reason="写块设备",
    ),
    DangerCommandRule(
        name="mkfs-format",
        matcher=r"\bmkfs(\.\w+)?\b",
        reason="格式化文件系统",
    ),
    DangerCommandRule(
        name="fdisk-write",
        matcher=r"\bfdisk\s+/dev/",
        reason="写分区表",
    ),
    DangerCommandRule(
        name="chmod-zero-root",
        matcher=r"\bchmod\s+(-R\s+)?0+\s+/(\s|$)",
        reason="把根目录权限置零",
    ),
    DangerCommandRule(
        name="chmod-zero-system-dirs",
        matcher=(
            r"\bchmod\s+(-R\s+)?0+\s+"
            r"/(etc|usr|bin|sbin|var|root|home|boot|lib|opt)(\s|/|$)"
        ),
        reason="递归归零关键系统目录权限",
    ),
    DangerCommandRule(
        name="pipe-to-shell",
        matcher=r"^\s*(sh|bash|zsh|dash|ksh)\s*(-[a-zA-Z]*)?\s*$",
        reason="执行经管道传入的脚本",
    ),
    DangerCommandRule(
        name="chown-recursive-root",
        matcher=r"\bchown\s+(-R\s+)?\S+\s+/(\s|$)",
        reason="递归修改根目录所有者",
    ),
    DangerCommandRule(
        name="fork-bomb",
        matcher=r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        reason="fork bomb",
    ),
    DangerCommandRule(
        name="git-dir-destroy",
        matcher=(
            r"^\s*(?:rm|mv)\b"
            r"(?=.*(?<!\S)(?:\S*/)?\.git(?:/[^\s]*)?(?=\s|$))"
        ),
        reason="删除或移动 Git 内部目录",
    ),
)


DEFAULT_DESTRUCTIVE_ALWAYS_ASK: tuple[DangerCommandRule, ...] = (
    DangerCommandRule(
        name="rm-recursive",
        matcher=r"^rm\s+(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b",
        reason="递归删除：必须显式审批",
    ),
    DangerCommandRule(name="shred", matcher=r"\bshred\b", reason="安全擦除：不可逆"),
    DangerCommandRule(name="srm", matcher=r"\bsrm\b", reason="安全删除：不可逆"),
)


DEFAULT_SENSITIVE_PATHS: tuple[DangerPathRule, ...] = (
    DangerPathRule(
        name="ssh-material",
        matcher="~/.ssh/",
        ops=frozenset({"read", "write"}),
        reason="SSH 凭据与主机信任关系",
    ),
    DangerPathRule(
        name="gnupg-material",
        matcher="~/.gnupg/",
        ops=frozenset({"read", "write"}),
        reason="GnuPG 私钥",
    ),
    DangerPathRule(
        name="aws-credentials",
        matcher="~/.aws/credentials",
        ops=frozenset({"read", "write"}),
        reason="AWS 凭据",
    ),
    DangerPathRule(
        name="netrc",
        matcher="~/.netrc",
        ops=frozenset({"read", "write"}),
        reason="网络资源凭据",
    ),
    DangerPathRule(
        name="docker-config",
        matcher="~/.docker/config.json",
        ops=frozenset({"read", "write"}),
        reason="Docker registry 凭据",
    ),
    DangerPathRule(
        name="git-internal",
        matcher=".git/",
        match_mode="project_relative",
        ops=frozenset({"write"}),
        reason="Git 内部目录，写入会破坏仓库完整性",
    ),
)


__all__ = [
    "DEFAULT_DESTRUCTIVE_ALWAYS_ASK",
    "DEFAULT_HARD_DENY_COMMANDS",
    "DEFAULT_SENSITIVE_PATHS",
    "DangerCommandRule",
    "DangerPathRule",
]
