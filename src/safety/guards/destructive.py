"""safety — DestructiveForceAskGuard（决策链第 ② 层）。

位于 HardBlock 之后、TrustResolver 之前：命中即跳过 Trust 直接进入
ConsentResolver（elevated 审批），**无视任何已有 grant**。

链路顺序：

    HardBlock → Boundary → **DestructiveForceAsk** → Trust → Consent

设计要点：

- **不拒绝，只强制审批**：与 HardBlock（硬拒绝）不同，本 guard 把命中的
  请求标记为"必须人工确认"，用户仍可选择同意（但每次都要问）。
- **段级匹配**：复用 :func:`hard_block._split_shell_segments` 的拆段逻辑，
  对 ``&&`` / ``||`` / ``;`` / ``|`` 拆段后逐段 ``re.search``。
- **仅消费 shell 命令**：非 ``run_shell`` 工具直接返回 ``None``。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from safety.default_rules import DEFAULT_DESTRUCTIVE_ALWAYS_ASK
from safety.types import BoundaryScope, DestructivePattern

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from config_loader.models import Config
    from core.contracts import ApprovalRequest

# 复用 hard_block 的 shell 分隔符正则
_SHELL_SEGMENT_SPLITTER = re.compile(r"(?:&&|\|\||;|\|)")

_SHELL_TOOLS: frozenset[str] = frozenset({"run_shell"})


class DestructiveForceAskGuard:
    """决策链第 ② 层：destructive 命令强制 consent。

    ``matches(request)`` 返回 ``True`` 表示命中 destructive 规则，
    调用方（SafetyDecisionEngine）应跳过 TrustResolver 直接进入
    ConsentResolver。

    返回 ``False`` 表示不命中，决策链继续走 TrustResolver。
    """

    def __init__(
        self,
        patterns: tuple[DestructivePattern, ...],
    ) -> None:
        self._patterns = patterns
        # 预编译 regex（segment_regex 模式）
        self._compiled: list[tuple[DestructivePattern, re.Pattern[str]]] = []
        for p in patterns:
            if p.match_mode == "segment_regex":
                try:
                    self._compiled.append((p, re.compile(p.matcher)))
                except re.error as exc:
                    logger.warning(
                        "DestructiveForceAskGuard: skipping rule %r — invalid regex %r: %s",
                        p.name,
                        p.matcher,
                        exc,
                    )

    @classmethod
    def from_config(cls, config: Config) -> DestructiveForceAskGuard:
        """从 Config 装配：默认规则 + 用户 yaml 追加规则。

        用户 yaml 中 ``safety.destructive_always_ask`` 字段追加到默认规则
        之后（不替换）。当前 Config 未提供该字段，预留接口。
        """
        # 默认规则（不可被用户降级）
        patterns = list(DEFAULT_DESTRUCTIVE_ALWAYS_ASK)
        # 用户追加（预留，当前 Config 无此字段）
        user_patterns: list[Any] = getattr(
            getattr(config, "safety", None), "destructive_always_ask", []
        )
        for up in user_patterns:
            patterns.append(
                DestructivePattern(
                    name=up.name,
                    matcher=up.matcher,
                    match_mode=up.match_mode,
                    boundary_scope=BoundaryScope(up.boundary_scope),
                    reason=up.reason,
                )
            )
        return cls(tuple(patterns))

    def matches(self, request: ApprovalRequest) -> bool:
        """检查请求是否命中 destructive 规则。

        非 shell 工具直接返回 ``False``。shell 命令按段拆分后逐段检查。
        """
        return self.matched_rule(request) is not None

    def matched_rule(self, request: ApprovalRequest) -> DestructivePattern | None:
        """返回命中的第一条规则（用于 trace），不命中返回 ``None``。"""
        command = self._extract_command(request)
        if command is None:
            return None
        return self._find_matched_rule(command)

    @staticmethod
    def _extract_command(request: ApprovalRequest) -> str | None:
        """从 ApprovalRequest 提取有效 shell 命令，非 shell 或空命令返回 None。"""
        if request.tool_name not in _SHELL_TOOLS:
            return None
        command = (request.arguments or {}).get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        return command

    def _find_matched_rule(self, command: str) -> DestructivePattern | None:
        """段级匹配：按 shell 分隔符拆段，每段独立检查，返回命中的第一条规则。"""
        segments = _SHELL_SEGMENT_SPLITTER.split(command)
        for segment in segments:
            segment = segment.strip()
            if not segment:
                continue
            for pattern, compiled in self._compiled:
                if compiled.search(segment):
                    return pattern
        return None


__all__ = ["DestructiveForceAskGuard"]
