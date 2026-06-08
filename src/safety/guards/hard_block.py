"""safety v0.1.4 — HardBlockGuard（M2）。

主决策链第一道防线：命中 ``secrets`` / ``destructive`` / ``self-escalation``
直接 ``rejected``，不进 ``ConsentResolver``。

匹配维度：

- **路径匹配**（``path_prefix`` + ``effect=block``）
  - 先 ``Path.expanduser().resolve(strict=False)`` 解析 symlink / ``..``
  - 再前缀比对默认 ``DEFAULT_SENSITIVE_PATHS``（仅消费 ``effect=block``）
  - ``project_relative`` / ``glob`` 规则留给 ``BoundaryResolver`` / ``ConsentResolver`` 处理
- **命令匹配**（``segment_regex`` + ``hard_deny_commands``）
  - 先按 shell 分隔符 ``&&`` / ``||`` / ``;`` / ``|`` 切多个 segment
  - 每个 segment 独立用 ``re.search`` 比对 regex matcher
  - 防 ``pytest tests/ && rm -rf /`` 逃逸
- **ConfigSelfProtection 子集**
  - ``write_file`` 落在 ``~/.kongming/`` → 命中 ``kongming-global-config`` rule
  - ``write_file`` 目标是项目级 ``.kongming/config*`` 且 ``content`` 含 ``allow_writes:``
    + 根级路径前缀（``/``、``/etc``、``/usr``、``~``、``$HOME``）→ 命中
    ``config-self-escalation`` profile rule

约束：

- 运行时 ``boundary_kind`` 恒为 ``host``；本类不写 ``if sandbox`` 分支
- ``session/config`` grant 不能覆盖 hard_block（顺序保证：HardBlock 在主链路最前）
- 返回 :class:`ApprovalDecision`：``outcome=rejected`` 且 ``metadata`` 含
  ``decision_class=hard_block`` / ``matched_rule`` / ``reason`` / ``boundary_kind=host``
  / ``suggested_alternatives``。``stage="capability"`` 兼容字段保留。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.contracts import ApprovalDecision, ApprovalRequest
from infrastructure.config.models import (
    Config,
    SafetyHardDenyConfig,
)
from safety.approval.default_rules import (
    DEFAULT_HARD_DENY_COMMANDS,
    DEFAULT_SENSITIVE_PATHS,
)
from safety.approval.types import (
    ApprovalMetadataKeys,
    BoundaryScope,
    HardDenyCommand,
    RuntimeBoundaryContext,
    SensitivePathRule,
)

# ---------------------------------------------------------------------------
# 工具名分类
# ---------------------------------------------------------------------------

_READ_TOOLS: frozenset[str] = frozenset({"read_file", "list_dir"})
_WRITE_TOOLS: frozenset[str] = frozenset({"write_file"})
_SHELL_TOOLS: frozenset[str] = frozenset({"run_shell"})

# Shell 分隔符：&& / || / ; / |（按字符顺序处理 || 必须先于 |、&& 先于 &）
# 注意：管道 `|` 在某些场合是合法的数据流，但作为 hard-block 检查，仍然按 segment 切
# 防止 `cat foo.txt | rm -rf /` 这类逃逸。
_SHELL_SEGMENT_SPLITTER = re.compile(r"(?:&&|\|\||;|\|)")


# ConfigSelfProtection: 视为"扩大边界到根目录/HOME"的根级路径前缀
# 出现在 allow_writes / allow_tools_silent yaml 字段时构成自我提权
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

# 项目级 kongming 配置文件（write 落在此处 + content 含根级 allow_writes → escalation）
_KONGMING_CONFIG_FILENAMES: tuple[str, ...] = (
    ".kongming/config.yaml",
    ".kongming/config.yml",
    ".kongming/config.json",
    ".kongming/config.toml",
)


# ---------------------------------------------------------------------------
# 内部数据
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _HardBlockHit:
    """内部 evaluate 命中的临时结构。"""

    rule_name: str
    reason: str
    target_kind: str  # "path" / "command" / "config_escalation"
    target_value: str  # 规则 reason 文本
    suggested_alternatives: tuple[str, ...]


def _normalize_user_hard_deny(
    user_rules: list[SafetyHardDenyConfig],
) -> tuple[HardDenyCommand, ...]:
    """把 yaml 里用户追加的 hard_deny_commands 配置翻译成 HardDenyCommand 元组。"""
    return tuple(
        HardDenyCommand(
            name=cfg.name,
            matcher=cfg.matcher,
            match_mode=cfg.match_mode,
            boundary_scope=BoundaryScope(cfg.boundary_scope),
            reason=cfg.reason,
        )
        for cfg in user_rules
    )


# ---------------------------------------------------------------------------
# HardBlockGuard
# ---------------------------------------------------------------------------


class HardBlockGuard:
    """主决策链第一段：命中即拒绝，不进 ask。

    通过 :meth:`from_config` 构造，自动合并默认规则 + 用户 yaml 追加规则；
    通过 :meth:`evaluate` 在 :class:`ApprovalRequest` 上做检查，命中返回
    rejected :class:`ApprovalDecision`，不命中返回 ``None``。
    """

    def __init__(
        self,
        *,
        hard_deny_commands: tuple[HardDenyCommand, ...],
        sensitive_paths: tuple[SensitivePathRule, ...],
    ) -> None:
        self._hard_deny_commands = hard_deny_commands
        self._sensitive_paths = sensitive_paths
        # 预切：仅 effect=block + match_mode=path_prefix 的规则在本 guard 消费
        # project_relative / glob 留给 BoundaryResolver / ConsentResolver
        self._block_path_rules: tuple[SensitivePathRule, ...] = tuple(
            r for r in sensitive_paths if r.effect == "block" and r.match_mode == "path_prefix"
        )

    # ------------------------------------------------------------------
    # 装配入口
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Config) -> HardBlockGuard:
        """从 :class:`Config` 装配。

        合并策略（与 ``docs/safety-scope-v0.1.4/04-data-and-state.md`` 配置 schema 收敛原则一致）：

        - **追加而非覆盖**：用户 yaml 的 ``safety.hard_deny_commands`` /
          ``safety.sensitive_paths`` 追加在默认规则之后
        - 安全基线不可被降级
        """
        user_hard_deny = _normalize_user_hard_deny(list(config.safety.hard_deny_commands))
        user_sensitive_paths = _normalize_user_sensitive_paths(list(config.safety.sensitive_paths))
        return cls(
            hard_deny_commands=DEFAULT_HARD_DENY_COMMANDS + user_hard_deny,
            sensitive_paths=DEFAULT_SENSITIVE_PATHS + user_sensitive_paths,
        )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def evaluate(
        self,
        request: ApprovalRequest,
        runtime: RuntimeBoundaryContext,
    ) -> ApprovalDecision | None:
        """对一次 :class:`ApprovalRequest` 做硬阻断检查。

        Returns:
            - ``None``：未命中，调用方继续走下一层（``BoundaryResolver`` / ``TrustResolver``）
            - :class:`ApprovalDecision`（``outcome=rejected``）：命中硬阻断
        """
        tool_name = request.tool_name
        args = request.arguments

        hit: _HardBlockHit | None = None

        # 1. ConfigSelfProtection: write_file 编辑 .kongming/config*
        #    且内容尝试扩大默认安全边界 → 立即拒绝（在路径匹配前判定）
        if tool_name in _WRITE_TOOLS:
            hit = self._check_config_escalation(args)

        # 2. 路径匹配（read / write）
        if hit is None and tool_name in _READ_TOOLS:
            hit = self._match_path(_extract_path(args), op="read")

        if hit is None and tool_name in _WRITE_TOOLS:
            hit = self._match_path(_extract_path(args), op="write")

        # 3. 命令匹配（shell）
        if hit is None and tool_name in _SHELL_TOOLS:
            hit = self._match_command(_extract_command(args))

        if hit is None:
            return None

        return _build_rejected_decision(hit)

    # ------------------------------------------------------------------
    # 路径匹配（path_prefix + effect=block）
    # ------------------------------------------------------------------

    def _match_path(
        self,
        raw_path: str | None,
        *,
        op: str,
    ) -> _HardBlockHit | None:
        """尝试匹配敏感路径规则（仅消费 ``effect=block`` + ``path_prefix``）。

        Args:
            raw_path: ApprovalRequest.arguments 里的原始路径字符串。
            op: ``"read"`` 或 ``"write"``，用于过滤 ``ops`` 字段。

        Returns:
            命中规则信息或 ``None``。
        """
        if raw_path is None or not raw_path:
            return None

        try:
            resolved = Path(raw_path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            # 极端路径解析失败时不放行，转为命中通用 reject
            return _HardBlockHit(
                rule_name="path-resolve-failed",
                reason=f"无法解析路径 {raw_path!r}（可能含非法字符 / 循环 symlink）",
                target_kind="path",
                target_value=str(raw_path),
                suggested_alternatives=("建议改用绝对路径或者 ~/scratch/ 下的简单路径",),
            )

        for rule in self._block_path_rules:
            if op not in rule.ops:
                continue
            try:
                rule_path = Path(rule.matcher).expanduser().resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            if _is_under(resolved, rule_path):
                return _HardBlockHit(
                    rule_name=rule.name,
                    reason=rule.reason,
                    target_kind="path",
                    target_value=str(resolved),
                    suggested_alternatives=_alternatives_for_path(rule),
                )

        return None

    # ------------------------------------------------------------------
    # 命令匹配（segment_regex）
    # ------------------------------------------------------------------

    def _match_command(self, raw_cmd: str | None) -> _HardBlockHit | None:
        """对 shell 命令做 regex 匹配。

        策略：

        1. **整段匹配**：先用整条命令字符串做一次 regex search，
           覆盖跨 segment 的特殊字符串（如 fork bomb ``:(){ :|:& };:``，
           其内部含 ``|`` / ``;`` 反而让 segment 切分破坏完整性）。
        2. **segment 匹配**：再按 ``&&`` / ``||`` / ``;`` / ``|`` 切多段，
           每段单独跑 regex，防止 ``pytest && rm -rf /`` 这类逃逸。

        两层是 OR 关系：任一命中即拒绝。

        Args:
            raw_cmd: ApprovalRequest.arguments['command'] 原始字符串。

        Returns:
            命中规则信息或 ``None``。
        """
        if raw_cmd is None or not raw_cmd.strip():
            return None

        regex_rules: tuple[HardDenyCommand, ...] = tuple(
            r for r in self._hard_deny_commands if r.match_mode == "segment_regex"
        )

        # 第一层：整段匹配
        whole = raw_cmd.strip()
        for rule in regex_rules:
            if re.search(rule.matcher, whole):
                return _HardBlockHit(
                    rule_name=rule.name,
                    reason=rule.reason,
                    target_kind="command",
                    target_value=whole,
                    suggested_alternatives=_alternatives_for_command(rule),
                )

        # 第二层：segment 切分匹配（防 segment 逃逸）
        segments = _split_shell_segments(raw_cmd)
        for segment in segments:
            normalized = segment.strip()
            if not normalized:
                continue
            for rule in regex_rules:
                if re.search(rule.matcher, normalized):
                    return _HardBlockHit(
                        rule_name=rule.name,
                        reason=rule.reason,
                        target_kind="command",
                        target_value=normalized,
                        suggested_alternatives=_alternatives_for_command(rule),
                    )
        return None

    # ------------------------------------------------------------------
    # ConfigSelfProtection: 扩大边界到根级路径
    # ------------------------------------------------------------------

    def _check_config_escalation(
        self,
        args: dict[str, Any],
    ) -> _HardBlockHit | None:
        """检查写入 .kongming/config* 是否扩大默认安全边界。

        实现策略（v0.1.4 简化版）：

        1. 仅在 ``write_file`` 工具触发时检查
        2. 路径需以 ``.kongming/config`` 开头（无论扩展名）
        3. ``content`` 字段中含 ``allow_writes:`` 关键词
        4. 同一行 / 邻近上下文中含根级路径前缀（``/`` / ``/etc`` / ``~`` / ``$HOME`` 等）

        已知 gap：不解析完整 yaml；用户用 multi-line list 写法可能绕过。
        本次接受，文档化已知不完美。
        """
        raw_path = _extract_path(args)
        content = args.get("content")
        if raw_path is None or content is None or not isinstance(content, str):
            return None

        # 路径归一化为相对项目（path_prefix 比对：`.kongming/config` 必须出现在 path 中）
        # 不做 resolve，避免误绕过——直接看用户传入的相对/绝对路径文本。
        normalized_path = str(raw_path)
        if not any(suffix in normalized_path for suffix in _KONGMING_CONFIG_FILENAMES):
            return None

        # 关键词扫描：必须同时含 allow_writes: 和 root-like prefix
        if "allow_writes:" not in content and "allow_tools_silent:" not in content:
            return None

        for prefix in _ROOT_LIKE_PATH_PREFIXES:
            # 用引号 / 列表元素形式定位（"/" / '/' / - / 等）
            # 简化：只要内容里出现 prefix 紧跟引号、空格、或行尾即视为命中
            patterns = (
                f'"{prefix}"',
                f"'{prefix}'",
                f"- {prefix}",
                f"-{prefix}",  # `-/etc` 这种紧贴写法
                f"\n{prefix}\n",
            )
            if any(p in content for p in patterns):
                return _HardBlockHit(
                    rule_name="config-self-escalation",
                    reason="试图扩大 agent 默认安全边界到根目录或 HOME",
                    target_kind="config_escalation",
                    target_value=normalized_path,
                    suggested_alternatives=(
                        "建议改为 elevated ask 提示用户手工编辑 .kongming/config.yaml",
                        "建议把 allow_writes 限定到 .kongming/work/ 等低风险目录",
                    ),
                )

        return None


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _normalize_user_sensitive_paths(
    user_rules: list[Any],
) -> tuple[SensitivePathRule, ...]:
    """把 yaml 里用户追加的 sensitive_paths 配置翻译成 SensitivePathRule 元组。"""
    out: list[SensitivePathRule] = []
    for cfg in user_rules:
        out.append(
            SensitivePathRule(
                name=cfg.name,
                matcher=cfg.matcher,
                match_mode=cfg.match_mode,
                ops=frozenset(cfg.ops),
                effect=cfg.effect,
                boundary_scope=BoundaryScope(cfg.boundary_scope),
                reason=cfg.reason,
            )
        )
    return tuple(out)


def _extract_path(args: dict[str, Any]) -> str | None:
    """从 ApprovalRequest.arguments 里取 path 字段。"""
    raw = args.get("path")
    if isinstance(raw, str):
        return raw
    return None


def _extract_command(args: dict[str, Any]) -> str | None:
    """从 ApprovalRequest.arguments 里取 command 字段。"""
    raw = args.get("command")
    if isinstance(raw, str):
        return raw
    return None


def _strip_quote_chars(raw: str) -> str:
    """把所有 ``"`` / ``'`` / ``\\``（含转义）替换成空格。

    这是 hard_block segment 切分前的预处理：把引号当成"透明"处理，
    避免 ``echo "hello && rm -rf /"`` 这类把命令藏进引号绕过检查。
    本预处理仅用于 hard_block 的 regex 匹配，不影响实际命令执行语义。
    """
    return raw.replace('"', " ").replace("'", " ").replace("\\", " ")


def _split_shell_segments(raw_cmd: str) -> list[str]:
    """按 shell 分隔符切多 segment（先剥引号防绕过）。

    流程：``_strip_quote_chars`` 把引号转空白 → regex split by ``&&|||\\|;``。

    已知 gap：
    - 不解析进程替换 ``<(...)``、命令替换 ``$(...)``
    - 不区分管道 ``|`` 是数据流还是逃逸（一律切段后逐段检查）

    引号绕过场景已通过 ``_strip_quote_chars`` 修复，参见单测
    ``test_scenario_quoted_command_does_not_evade``。
    """
    cleaned = _strip_quote_chars(raw_cmd)
    return _SHELL_SEGMENT_SPLITTER.split(cleaned)


def _is_under(target: Path, prefix: Path) -> bool:
    """``target`` 是否在 ``prefix`` 目录下（含 prefix 自身）。

    使用 ``Path.is_relative_to``（Python 3.9+）。
    """
    try:
        return target == prefix or target.is_relative_to(prefix)
    except ValueError:
        return False


def _alternatives_for_path(rule: SensitivePathRule) -> tuple[str, ...]:
    """根据规则名生成 path 类拒绝的备选建议。"""
    name = rule.name
    if name in {"ssh-material", "gnupg-material", "aws-credentials", "netrc"}:
        return (
            "建议改写到 ~/.kongming/work/ 而非凭据目录",
            "建议把内容输出给用户，由用户手工写入凭据文件",
        )
    if name == "kongming-global-config":
        return (
            "建议改写到项目级 .kongming/config.yaml（需 elevated 审批）",
            "建议改用 elevated ask 提示用户手工编辑 ~/.kongming/",
        )
    if name == "docker-config":
        return (
            "建议把 registry 凭据输出给用户手工配置",
            "建议改用 docker login 命令而非直接编辑 config.json",
        )
    return (f"建议避开 {rule.matcher}，改写到工作目录（如 .kongming/work/）",)


def _alternatives_for_command(rule: HardDenyCommand) -> tuple[str, ...]:
    """根据规则名生成 command 类拒绝的备选建议。"""
    name = rule.name
    if name == "host-root-delete":
        return (
            "建议改用 rm -rf <具体子目录> 而非根目录",
            "建议先列出待删除文件再确认（ls -la <dir>）",
        )
    if name == "rm-rf-no-preserve-root":
        return (
            "建议去掉 --no-preserve-root 选项",
            "建议改用具体子目录路径，让系统默认保护根目录生效",
        )
    if name == "raw-disk-write":
        return (
            "建议改用普通文件路径而非块设备",
            "建议把数据先写入文件，再由用户手工 dd 到目标设备",
        )
    if name in {"mkfs-format", "fdisk-write"}:
        return ("建议把操作输出给用户，由用户手工执行格式化 / 分区命令",)
    if name == "fork-bomb":
        return ("建议拆分任务，避免无限递归 fork",)
    if name in {"chmod-zero-root", "chown-recursive-root"}:
        return (
            "建议改用具体子路径而非根目录",
            "建议提供期望的最终权限并由用户手工执行",
        )
    return (f"建议避免 {rule.name} 类操作；可改用更小作用域的命令",)


def _build_rejected_decision(hit: _HardBlockHit) -> ApprovalDecision:
    """把命中信息打包成结构化 :class:`ApprovalDecision` reject。"""
    metadata: dict[str, Any] = {
        # v0.1.3 兼容：hard_block 映射到旧 capability stage
        ApprovalMetadataKeys.STAGE: "capability",
        ApprovalMetadataKeys.DECISION_CLASS: "hard_block",
        # hard_block 来源恒为内置基线（不可被任何 grant 降级），统一标 intrinsic
        ApprovalMetadataKeys.DECISION_SOURCE: "intrinsic",
        ApprovalMetadataKeys.MATCHED_RULE: hit.rule_name,
        ApprovalMetadataKeys.REASON: hit.reason,
        ApprovalMetadataKeys.BOUNDARY_KIND: "host",
        ApprovalMetadataKeys.SUGGESTED_ALTERNATIVES: list(hit.suggested_alternatives),
    }
    human_reason = f"[hard_block:{hit.rule_name}] {hit.reason}"
    if hit.target_value:
        human_reason += f" (target={hit.target_value!r})"
    return ApprovalDecision(
        outcome="rejected",
        reason=human_reason,
        metadata=metadata,
    )


__all__ = [
    "HardBlockGuard",
]
