"""safety v0.1.4 — ConsentResolver（M4）。

主决策链第三段（HardBlock → Boundary → Trust → **Consent**）：把"需要人决策"的
:class:`core.contracts.ApprovalRequest` 分流到 ``standard`` 或 ``elevated`` ask，
然后委托给上游注入的 :class:`core.contracts.ApprovalProvider`（M5 的
``InteractiveApproval`` 升级版本）拿真实人决策。

职责范围：

- 消费 :data:`safety.default_rules.DEFAULT_APPROVAL_REQUIRED_COMMANDS` 表
  + 用户 yaml ``safety.approval_required_commands`` 追加规则
- 消费 :data:`safety.default_rules.DEFAULT_SENSITIVE_PATHS` 中 ``effect=elevated``
  的条目，识别 read/write 操作的敏感路径
- ConfigSelfProtection elevated 子集：``CLAUDE.md`` / ``AGENTS.md`` /
  ``.kongming/config*`` / ``.kongming/safety/`` / ``pyproject.toml [tool.kongming.safety]``
- 派生 ``confirm_token``（sha256(call_id + matched_rule)[:8]）；通过 ``request.metadata``
  把 ``policy_hint="elevated"`` + ``confirm_token`` 透传给 InteractiveApproval

不负责：

- 沙盒分支判定：``boundary_kind`` 恒 ``host``
- BLOCKED zone 处理：HardBlock 已拦，本类只做兜底 reject + ``assert``
- 实际人机交互：UI / 三按钮 / typed confirm 都由 M5 InteractiveApproval 实现
- session/config grant 的查询与写回：那是 TrustResolver / GrantStore 的责任

返回结构（命中 standard / elevated）：

- ``outcome``：透传 InteractiveApproval 决策
- ``metadata``：

  - ``decision_class="explicit_consent"``
  - ``decision_source="standard" | "elevated"``
  - ``matched_rule="..."``
  - ``reason="..."``
  - ``stage="permission" | "approval"`` （兼容 v0.1.3）
  - ``boundary_kind="host"``
  - ``grant_scope`` 透传 InteractiveApproval 返回的字段（如有）
  - ``confirm_token`` 仅 elevated 时携带
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from config_loader.models import (
    Config,
    SafetyApprovalRequiredConfig,
    SafetySensitivePathConfig,
    SafetySkillCallConfig,
)
from core.contracts import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
)
from safety.default_rules import (
    DEFAULT_APPROVAL_REQUIRED_COMMANDS,
    DEFAULT_SENSITIVE_PATHS,
    DEFAULT_SKILL_CALL_RULES,
)
from safety.types import (
    ApprovalMetadataKeys,
    ApprovalRequiredCommand,
    BoundaryDecision,
    BoundaryScope,
    BoundaryZone,
    RuntimeBoundaryContext,
    SensitivePathRule,
    SkillCallRule,
)

# ---------------------------------------------------------------------------
# 工具名分类（与 BoundaryResolver / HardBlockGuard 保持一致）
# ---------------------------------------------------------------------------

_READ_TOOLS: frozenset[str] = frozenset({"read_file", "list_dir"})
_WRITE_TOOLS: frozenset[str] = frozenset({"write_file"})
_SHELL_TOOLS: frozenset[str] = frozenset({"run_shell"})
_SKILL_TOOLS: frozenset[str] = frozenset({"skill"})
_AGENT_WORKFLOW_TOOLS: frozenset[str] = frozenset({"run_agent_workflow", "run_parallel_subagents"})


# ---------------------------------------------------------------------------
# Profile 匹配：把高层动作意图（git_push / git_reset_hard / ...）翻译成对命令字符串
# 的探测逻辑。每个 profile 对应一个轻量正则。
#
# 这里只做 v0.1.4 内置 profile 的最小集；用户 yaml 追加的 profile 命名走相同探测
# 链路（同一个 matcher 字符串），未识别的 profile 名 fallback 为返回 False。
# ---------------------------------------------------------------------------

_PROFILE_PATTERNS: dict[str, re.Pattern[str]] = {
    # `git push ...` 出现即命中
    "git_push": re.compile(r"\bgit\s+push\b"),
    # `git reset --hard` 出现即命中
    "git_reset_hard": re.compile(r"\bgit\s+reset\s+(--hard|-{1,2}hard\b)"),
}


def _profile_matches(profile_id: str, command: str) -> bool:
    """命令字符串是否命中给定 profile。

    未识别的 profile id 返回 False（保守 fallback；workspace_external_write 等
    要求路径上下文的 profile 由 BoundaryResolver / TrustResolver 推导，不在
    本模块判定）。
    """
    pattern = _PROFILE_PATTERNS.get(profile_id)
    if pattern is None:
        return False
    return pattern.search(command) is not None


# ---------------------------------------------------------------------------
# ConfigSelfProtection elevated 子集：项目级敏感配置 / 行为约束
# ---------------------------------------------------------------------------

# 项目根下"自我配置"文件名集合：写入这些路径需 elevated。
_CONFIG_SELF_PROTECTION_FILES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
)

# 项目根下"自我配置"路径前缀（含 .kongming/safety/ 目录前缀）。
_CONFIG_SELF_PROTECTION_DIR_PREFIXES: tuple[str, ...] = (
    ".kongming/safety/",
    ".kongming/safety",  # 没有 trailing slash 的容错
)

# 项目根下 .kongming/config.{yaml,yml,json,toml} 文件名集合。
_KONGMING_CONFIG_BASENAMES: tuple[str, ...] = (
    "config.yaml",
    "config.yml",
    "config.json",
    "config.toml",
)


# ---------------------------------------------------------------------------
# 命中信息（内部传值，不对外暴露）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ConsentHit:
    """consent.evaluate 内部命中结构。

    - ``severity``：``"standard"`` 或 ``"elevated"``
    - ``matched_rule``：写入 metadata.matched_rule
    - ``reason``：人读理由（trace + reject reason）
    - ``target_value``：命中目标（path 或 command 文本）
    """

    severity: str
    matched_rule: str
    reason: str
    target_value: str


# ---------------------------------------------------------------------------
# ConsentResolver
# ---------------------------------------------------------------------------


class ConsentResolver:
    """主决策链终点 guard：分流 standard / elevated 后委托 InteractiveApproval。

    构造方式优先用 :meth:`from_config`，由它从 :class:`Config` 合并默认 +
    用户规则，再注入 :class:`InteractiveApproval`（M5 升级版本）。

    返回的 :class:`ApprovalDecision` 总是装饰过 metadata 的：本 guard 是终点
    guard，**永远返回 ApprovalDecision，不返回 None**。
    """

    def __init__(
        self,
        *,
        approval_required_commands: tuple[ApprovalRequiredCommand, ...],
        sensitive_paths: tuple[SensitivePathRule, ...],
        skill_call_rules: tuple[SkillCallRule, ...] = (),
        interactive_approval: ApprovalProvider,
    ) -> None:
        self._approval_required_commands = approval_required_commands
        self._interactive_approval = interactive_approval
        # 预切：仅 effect=elevated 的规则在本 guard 消费
        # （effect=block 由 HardBlock 处理，effect=allow 由 BoundaryResolver 处理）
        self._elevated_path_rules: tuple[SensitivePathRule, ...] = tuple(
            r
            for r in sensitive_paths
            if r.effect == "elevated" and r.boundary_scope is not BoundaryScope.SANDBOX
        )
        # skill_call_rules 按 effect 分桶（v0.1.6 skill-system 模块 C）。
        # 优先级链 block > elevated > allow > standard fallback；SANDBOX 规则
        # 直接过滤掉，保持与 approval_required_commands / sensitive_paths 一致。
        self._skill_block: tuple[SkillCallRule, ...] = tuple(
            r
            for r in skill_call_rules
            if r.effect == "block" and r.boundary_scope is not BoundaryScope.SANDBOX
        )
        self._skill_elevated: tuple[SkillCallRule, ...] = tuple(
            r
            for r in skill_call_rules
            if r.effect == "elevated" and r.boundary_scope is not BoundaryScope.SANDBOX
        )
        self._skill_allow: tuple[SkillCallRule, ...] = tuple(
            r
            for r in skill_call_rules
            if r.effect == "allow" and r.boundary_scope is not BoundaryScope.SANDBOX
        )

    # ------------------------------------------------------------------
    # 装配入口
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        interactive_approval: ApprovalProvider,
    ) -> ConsentResolver:
        """从 :class:`Config` 构造，合并默认规则 + 用户 yaml 追加规则。

        Args:
            config: 全局配置实例。
            interactive_approval: 真正承担人机交互的 ApprovalProvider 实现
                （生产环境为 :class:`tools.approval.InteractiveApproval` 的 M5
                升级版本；测试可注入 stub）。
        """
        user_approval_required = _normalize_user_approval_required_commands(
            list(config.safety.approval_required_commands),
        )
        user_sensitive_paths = _normalize_user_sensitive_paths(
            list(config.safety.sensitive_paths),
        )
        user_skill_call_rules = _normalize_user_skill_call_rules(
            list(config.safety.skill_call_rules),
        )
        return cls(
            approval_required_commands=DEFAULT_APPROVAL_REQUIRED_COMMANDS + user_approval_required,
            sensitive_paths=DEFAULT_SENSITIVE_PATHS + user_sensitive_paths,
            skill_call_rules=DEFAULT_SKILL_CALL_RULES + user_skill_call_rules,
            interactive_approval=interactive_approval,
        )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        request: ApprovalRequest,
        boundary: BoundaryDecision,
        runtime: RuntimeBoundaryContext,
    ) -> ApprovalDecision:
        """对一次 :class:`ApprovalRequest` 做 consent 分流并发起人审批。

        Args:
            request: 已通过 HardBlock + Boundary + Trust 的请求。
            boundary: BoundaryResolver 输出（zone + matched_rule）。
            runtime: runtime 上下文（boundary_kind 恒为 host）。

        Returns:
            装饰过 metadata 的 :class:`ApprovalDecision`：

            - ``outcome``：透传 InteractiveApproval 决策（approved / rejected /
              cancelled）
            - ``metadata``：必含 ``decision_class="explicit_consent"`` /
              ``decision_source`` / ``matched_rule`` / ``reason`` / ``stage`` /
              ``boundary_kind="host"``；elevated 还含 ``confirm_token``
        """
        # 1. BLOCKED zone：HardBlock 应该已拦截，这里仅作 defensive fallback
        if boundary.zone is BoundaryZone.BLOCKED:
            assert False, (  # noqa: B011 -- 这是设计意图：决策链顺序保证此路径不可达
                "BoundaryZone.BLOCKED reached ConsentResolver; HardBlock must intercept first"
            )

        # 2. 决策分流
        hit = self._classify(request)

        # 2.5. silent_allow 分流（v0.1.6 skill-system 模块 C）：
        # skill_call_rules 中 effect=allow 命中后跳过 InteractiveApproval，
        # 直接构造 silent_allow 决策。仅在 _classify_skill 路径产生 severity="allow"，
        # 其他主链路径（shell / write / read）不会走到这里。
        if hit.severity == "allow":
            return _build_silent_allow_decision(hit, request=request)

        # 3. 派生 confirm_token（仅 elevated）
        confirm_token: str | None = None
        if hit.severity == "elevated":
            confirm_token = _derive_confirm_token(request.call_id, hit.matched_rule)

        # 4. 构造 enriched request：注入 policy_hint / severity / confirm_token
        enriched_metadata: dict[str, Any] = {**request.metadata}
        enriched_metadata["policy_hint"] = hit.severity
        enriched_metadata["severity"] = hit.severity
        if confirm_token is not None:
            enriched_metadata["confirm_token"] = confirm_token
        enriched_request = replace(request, metadata=enriched_metadata)

        # 5. 委托 InteractiveApproval 做实际人机交互
        downstream_decision = await self._interactive_approval.decide(enriched_request)

        # 6. 装饰 metadata 后返回
        return _decorate_decision(
            downstream_decision,
            hit=hit,
            confirm_token=confirm_token,
        )

    # ------------------------------------------------------------------
    # 决策分流
    # ------------------------------------------------------------------

    def _classify(self, request: ApprovalRequest) -> _ConsentHit:
        """根据 tool_name + args 决定 severity + matched_rule。

        分流策略（顺序为优先级）：

        1. shell 命令：消费 approval_required_commands 表
           - 命中 segment_regex 或 profile → severity 由规则决定
           - 不命中 → severity=standard，matched_rule="shell-command-default"
        2. write_file：先看 ConfigSelfProtection elevated 子集（包含
           pyproject.toml 段级关键词检测），再看 sensitive_paths(elevated)
        3. read_file / list_dir：仅看 sensitive_paths(elevated)
        4. fallback：severity=standard
        """
        tool = request.tool_name
        args = request.arguments
        if tool in _SKILL_TOOLS:
            return self._classify_skill(args)

        if tool in _AGENT_WORKFLOW_TOOLS:
            return _ConsentHit(
                severity="standard",
                matched_rule="agent-workflow-default",
                reason="Agent workflow 编排工具，需用户确认",
                target_value=tool,
            )

        if tool in _SHELL_TOOLS:
            return self._classify_command(args)

        if tool in _WRITE_TOOLS:
            # write 优先看 ConfigSelfProtection（含 pyproject.toml 段级）
            self_hit = self._match_config_self_protection(args)
            if self_hit is not None:
                return self_hit
            # 再看 sensitive_paths(elevated)
            sens_hit = self._match_sensitive_path(args, op="write")
            if sens_hit is not None:
                return sens_hit
            return _ConsentHit(
                severity="standard",
                matched_rule="write-default",
                reason="工作区外或非敏感写入，需用户确认",
                target_value=str(args.get("path", "")),
            )

        if tool in _READ_TOOLS:
            sens_hit = self._match_sensitive_path(args, op="read")
            if sens_hit is not None:
                return sens_hit
            return _ConsentHit(
                severity="standard",
                matched_rule="read-default",
                reason="非敏感路径读取，需用户确认",
                target_value=str(args.get("path", "")),
            )

        # 其他未知工具：保守 standard
        return _ConsentHit(
            severity="standard",
            matched_rule="unknown-tool-default",
            reason=f"未知工具 {tool}，保守要求人工确认",
            target_value="",
        )

    # ------------------------------------------------------------------
    # 子分支：command（消费 approval_required_commands）
    # ------------------------------------------------------------------

    def _classify_command(self, args: dict[str, Any]) -> _ConsentHit:
        """对 ``run_shell`` 命令做规则匹配。

        - segment_regex：用 re.search（不切 segment；hard_block 已切过，consent
          层只判别"动作意图"，整段一次 search 已足够）
        - profile：调用 :func:`_profile_matches`
        """
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return _ConsentHit(
                severity="standard",
                matched_rule="shell-empty-command",
                reason="空命令，保守要求人工确认",
                target_value="",
            )

        for rule in self._approval_required_commands:
            if rule.boundary_scope is BoundaryScope.SANDBOX:
                continue  # 本次实现不接受 sandbox-only 规则
            matched = False
            if rule.match_mode == "segment_regex":
                try:
                    matched = re.search(rule.matcher, command) is not None
                except re.error:
                    matched = False
            elif rule.match_mode == "profile":
                matched = _profile_matches(rule.matcher, command)
            if matched:
                return _ConsentHit(
                    severity=rule.severity,
                    matched_rule=rule.name,
                    reason=rule.reason,
                    target_value=command.strip(),
                )

        # 不命中：standard fallback
        return _ConsentHit(
            severity="standard",
            matched_rule="shell-command-default",
            reason="未识别的 shell 命令，保守要求人工确认",
            target_value=command.strip(),
        )

    # ------------------------------------------------------------------
    # 子分支：skill（消费 skill_call_rules）
    # ------------------------------------------------------------------

    def _classify_skill(self, args: dict[str, Any]) -> _ConsentHit:
        """对 ``skill`` 工具调用做规则匹配。

        优先级链：``block`` > ``elevated`` > ``allow`` > ``standard`` fallback。

        - 命中 ``block``：返回 ``severity="elevated"``（让下游 InteractiveApproval
          以 elevated 强度展示并默认拒绝；reason 前缀 ``block:`` 可读区分）
        - 命中 ``elevated``：返回 ``severity="elevated"``
        - 命中 ``allow``：返回 ``severity="allow"``，触发 :meth:`evaluate` 的
          silent_allow 分流（不调 InteractiveApproval）
        - 未命中：``severity="standard"``，``matched_rule="skill-call-default"``
          —— deny-by-default：默认 ``DEFAULT_SKILL_CALL_RULES`` 为空 tuple，
          用户必须 yaml 显式 allow
        """
        skill_name = args.get("skill")
        if not isinstance(skill_name, str) or not skill_name.strip():
            return _ConsentHit(
                severity="standard",
                matched_rule="skill-empty-name",
                reason="skill 名为空，保守要求人工确认",
                target_value="",
            )

        for rule in self._skill_block:
            if _skill_name_matches(rule, skill_name):
                return _ConsentHit(
                    severity="elevated",
                    matched_rule=rule.name,
                    reason=f"block: {rule.reason}"
                    if rule.reason
                    else f"skill `{skill_name}` blocked by rule `{rule.name}`",
                    target_value=skill_name,
                )
        for rule in self._skill_elevated:
            if _skill_name_matches(rule, skill_name):
                return _ConsentHit(
                    severity="elevated",
                    matched_rule=rule.name,
                    reason=rule.reason
                    or f"skill `{skill_name}` requires elevated approval by rule `{rule.name}`",
                    target_value=skill_name,
                )
        for rule in self._skill_allow:
            if _skill_name_matches(rule, skill_name):
                return _ConsentHit(
                    severity="allow",
                    matched_rule=rule.name,
                    reason=rule.reason
                    or f"skill `{skill_name}` silent-allowed by rule `{rule.name}`",
                    target_value=skill_name,
                )

        return _ConsentHit(
            severity="standard",
            matched_rule="skill-call-default",
            reason="未配置 skill 规则，保守要求人工确认",
            target_value=skill_name,
        )

    # ------------------------------------------------------------------
    # 子分支：file（sensitive_paths elevated 匹配）
    # ------------------------------------------------------------------

    def _match_sensitive_path(
        self,
        args: dict[str, Any],
        *,
        op: str,
    ) -> _ConsentHit | None:
        """匹配 sensitive_paths 中 effect=elevated 的规则。

        - path_prefix：``Path(matcher).expanduser().resolve()`` 后做前缀比较
        - project_relative：以 project_root 为基比较（项目根从 Path.cwd 推导，
          known gap：测试可通过 tmp_path 切换 cwd）
        - glob：用 ``Path.match()`` 做文件名层 glob
        """
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return None

        try:
            resolved = Path(raw_path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None

        target_basename = resolved.name
        try:
            project_root = Path.cwd().resolve(strict=False)
        except (OSError, RuntimeError):
            project_root = Path("/")

        for rule in self._elevated_path_rules:
            if op not in rule.ops:
                continue
            if not _match_path_rule(rule, resolved, target_basename, project_root):
                continue
            return _ConsentHit(
                severity="elevated",
                matched_rule=rule.name,
                reason=rule.reason,
                target_value=str(resolved),
            )
        return None

    # ------------------------------------------------------------------
    # 子分支：ConfigSelfProtection elevated（write 路径或 pyproject.toml 段）
    # ------------------------------------------------------------------

    def _match_config_self_protection(
        self,
        args: dict[str, Any],
    ) -> _ConsentHit | None:
        """检查写入是否落在 ConfigSelfProtection elevated 范围。

        命中条件（任一）：

        1. ``CLAUDE.md`` / ``AGENTS.md``（项目根下）
        2. ``.kongming/config.{yaml,yml,json,toml}``
        3. ``.kongming/safety/`` 目录下任意文件
        4. ``pyproject.toml`` 且 content 中含 ``tool.kongming.safety`` 关键词
        """
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return None

        # 取 basename + 完整字符串，做轻量字符串比对（不依赖 cwd 推导项目根，
        # 因为 v0.1.4 的 ConfigSelfProtection 对 path 末段名比较敏感）
        try:
            normalized = Path(raw_path).expanduser()
        except (OSError, RuntimeError):
            return None

        basename = normalized.name
        path_str = str(normalized)

        # 1. CLAUDE.md / AGENTS.md（不限定项目根，只看 basename，已包含项目自有
        # 防误删——HardBlock 已禁 ~/.kongming/，这里只剩项目自有路径）
        if basename in _CONFIG_SELF_PROTECTION_FILES:
            return _ConsentHit(
                severity="elevated",
                matched_rule=f"config-self-protection-{basename}",
                reason=f"修改 agent 行为约束源 {basename}",
                target_value=path_str,
            )

        # 2. .kongming/config.{yaml,yml,json,toml}
        # 用字符串前缀检测：path 中含 ".kongming/config.<ext>"
        normalized_for_match = path_str.replace("\\", "/")
        for cfg_basename in _KONGMING_CONFIG_BASENAMES:
            if f".kongming/{cfg_basename}" in normalized_for_match:
                return _ConsentHit(
                    severity="elevated",
                    matched_rule=f"config-self-protection-{cfg_basename}",
                    reason=f"修改项目级 agent 配置 {cfg_basename}",
                    target_value=path_str,
                )

        # 3. .kongming/safety/...
        normalized_str = path_str.replace("\\", "/")
        for prefix in _CONFIG_SELF_PROTECTION_DIR_PREFIXES:
            if prefix in normalized_str:
                # 提取被改动的文件名
                idx = normalized_str.find(prefix)
                trailing = normalized_str[idx + len(prefix) :]
                tail_name = trailing.split("/")[0] if trailing else "safety"
                return _ConsentHit(
                    severity="elevated",
                    matched_rule=f"config-self-protection-safety-{tail_name or 'dir'}",
                    reason="修改项目级安全规则目录 .kongming/safety/",
                    target_value=path_str,
                )

        # 4. pyproject.toml + content 含 [tool.kongming.safety] 关键词
        if basename == "pyproject.toml":
            content = args.get("content")
            if isinstance(content, str) and _content_touches_safety_section(content):
                return _ConsentHit(
                    severity="elevated",
                    matched_rule="pyproject-safety-section",
                    reason="修改 pyproject.toml [tool.kongming.safety] 段",
                    target_value=path_str,
                )

        return None


# ---------------------------------------------------------------------------
# 模块级辅助函数
# ---------------------------------------------------------------------------


def _normalize_user_approval_required_commands(
    user_rules: list[SafetyApprovalRequiredConfig],
) -> tuple[ApprovalRequiredCommand, ...]:
    """yaml ``safety.approval_required_commands`` → :class:`ApprovalRequiredCommand`。"""
    return tuple(
        ApprovalRequiredCommand(
            name=cfg.name,
            matcher=cfg.matcher,
            match_mode=cfg.match_mode,
            severity=cfg.severity,
            boundary_scope=BoundaryScope(cfg.boundary_scope),
            reason=cfg.reason,
        )
        for cfg in user_rules
    )


def _normalize_user_sensitive_paths(
    user_rules: list[SafetySensitivePathConfig],
) -> tuple[SensitivePathRule, ...]:
    """yaml ``safety.sensitive_paths`` → :class:`SensitivePathRule`。"""
    return tuple(
        SensitivePathRule(
            name=cfg.name,
            matcher=cfg.matcher,
            match_mode=cfg.match_mode,
            ops=frozenset(cfg.ops),
            effect=cfg.effect,
            boundary_scope=BoundaryScope(cfg.boundary_scope),
            reason=cfg.reason,
        )
        for cfg in user_rules
    )


def _normalize_user_skill_call_rules(
    user_rules: list[SafetySkillCallConfig],
) -> tuple[SkillCallRule, ...]:
    """yaml ``safety.skill_call_rules`` → :class:`SkillCallRule`。

    boundary_scope 从 ``"any"`` / ``"host"`` 字符串转换为 :class:`BoundaryScope`
    枚举（``"sandbox"`` 已被 ``SafetySkillCallConfig._reject_sandbox_scope``
    在 yaml 加载阶段拦截，此处不会出现）。
    """
    return tuple(
        SkillCallRule(
            name=cfg.name,
            matcher=cfg.matcher,
            match_mode=cfg.match_mode,
            effect=cfg.effect,
            boundary_scope=BoundaryScope(cfg.boundary_scope),
            reason=cfg.reason,
        )
        for cfg in user_rules
    )


def _skill_name_matches(rule: SkillCallRule, skill_name: str) -> bool:
    """对单条 :class:`SkillCallRule` 做名字匹配（exact / prefix）。

    - ``exact``：``skill_name == rule.matcher``
    - ``prefix``：``skill_name.startswith(rule.matcher)``

    空 ``skill_name`` 永远不命中（防御性）；空 ``matcher`` 已在 yaml 加载阶段
    （:class:`SafetySkillCallConfig._reject_empty`）拒绝，因此 prefix 不会退化为
    全匹配黑洞。
    """
    if not skill_name:
        return False
    if rule.match_mode == "exact":
        return skill_name == rule.matcher
    if rule.match_mode == "prefix":
        return skill_name.startswith(rule.matcher)
    return False


def _match_path_rule(
    rule: SensitivePathRule,
    resolved_target: Path,
    target_basename: str,
    project_root: Path,
) -> bool:
    """对单条 SensitivePathRule 做匹配。

    - ``path_prefix``：把 matcher ``expanduser+resolve`` 后做前缀比较
    - ``project_relative``：把 matcher 拼到 project_root 后做前缀比较；
      不命中则 fallback 到字符串包含（容错 cwd 偏离项目根的场景）
    - ``glob``：用 :py:meth:`pathlib.Path.match` 做文件名层 glob
    """
    if rule.match_mode == "path_prefix":
        try:
            absolute_rule_path = Path(rule.matcher).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        return _is_under(resolved_target, absolute_rule_path)

    if rule.match_mode == "project_relative":
        # 主路径：以 project_root 为基拼接，再做前缀比较
        relative_rule_path: Path | None
        try:
            relative_rule_path = (project_root / rule.matcher).resolve(strict=False)
        except (OSError, RuntimeError):
            relative_rule_path = None
        if relative_rule_path is not None and _is_under(resolved_target, relative_rule_path):
            return True
        # fallback：path_str 中含 matcher（容错 cwd 偏离）
        target_str = str(resolved_target).replace("\\", "/")
        matcher_str = rule.matcher.replace("\\", "/").rstrip("/")
        # 形如 "AGENTS.md" 这类项目根直接子文件，需要严格匹配 basename，避免
        # 命中 "/path/with/AGENTS.md.bak" 这种假阳性
        if "/" not in matcher_str:
            return target_basename == matcher_str
        return matcher_str in target_str

    if rule.match_mode == "glob":
        # Path.match 做文件名层 glob：".env*" 命中 ".env"、".env.production"
        try:
            return Path(target_basename).match(rule.matcher)
        except ValueError:
            return False

    return False


def _is_under(target: Path, prefix: Path) -> bool:
    """``target`` 是否在 ``prefix`` 目录下（含 prefix 自身）。"""
    try:
        return target == prefix or target.is_relative_to(prefix)
    except ValueError:
        return False


def _content_touches_safety_section(content: str) -> bool:
    """检测 pyproject.toml 内容是否触及 ``[tool.kongming.safety]`` 段。

    简化策略：只做关键词检测（``tool.kongming.safety``）。
    Known gap：用户多步编辑可能绕过；本次接受。

    为略微提升精度，我们额外接受 ``[tool.kongming.safety]`` 段头形式。
    """
    return "tool.kongming.safety" in content


def _derive_confirm_token(call_id: str, matched_rule: str) -> str:
    """从 ``call_id + matched_rule`` 派生 8 位 confirm token。

    sha256 输入：``f"{call_id}|{matched_rule}"``，取 hex digest 前 8 位。
    与 InteractiveApproval 端的对比由 M5 实现；此处只负责派生 + 透传。
    """
    digest = hashlib.sha256(f"{call_id}|{matched_rule}".encode()).hexdigest()
    return digest[:8]


def _build_silent_allow_decision(
    hit: _ConsentHit,
    *,
    request: ApprovalRequest,
) -> ApprovalDecision:
    """skill_call_rules ``effect=allow`` 命中后构造 silent_allow ``ApprovalDecision``。

    与 :func:`safety.guards.trust._silent_allow` 字段约定一致；区别仅在
    ``decision_source`` 为 ``"config"``（来自 yaml 配置）。**不**写
    ``stage`` 字段（v0.1.3 没 silent_allow 概念，``_decorate_stage_compat``
    会为 silent_allow 留空）。
    """
    metadata: dict[str, Any] = {
        ApprovalMetadataKeys.DECISION_CLASS: "silent_allow",
        ApprovalMetadataKeys.DECISION_SOURCE: "config",
        ApprovalMetadataKeys.MATCHED_RULE: hit.matched_rule,
        ApprovalMetadataKeys.REASON: hit.reason,
        ApprovalMetadataKeys.BOUNDARY_KIND: "host",
    }
    return ApprovalDecision(
        outcome="approved",
        reason=f"[silent_allow:config] {hit.reason}",
        metadata=metadata,
    )


def _decorate_decision(
    downstream: ApprovalDecision,
    *,
    hit: _ConsentHit,
    confirm_token: str | None,
) -> ApprovalDecision:
    """把 InteractiveApproval 返回的决策装饰上 v0.1.4 metadata。

    - 透传 ``downstream.outcome`` / ``downstream.reason``
    - 透传 ``downstream.metadata`` 中的 ``grant_scope``（如 ``session`` / ``config``）
    - 注入 v0.1.4 标准 metadata 键
    """
    decorated: dict[str, Any] = dict(downstream.metadata)

    # v0.1.3 兼容字段
    decorated[ApprovalMetadataKeys.STAGE] = (
        "permission" if hit.severity == "standard" else "approval"
    )
    decorated[ApprovalMetadataKeys.DECISION_CLASS] = "explicit_consent"
    decorated[ApprovalMetadataKeys.DECISION_SOURCE] = hit.severity
    decorated[ApprovalMetadataKeys.MATCHED_RULE] = hit.matched_rule
    decorated[ApprovalMetadataKeys.REASON] = hit.reason
    decorated[ApprovalMetadataKeys.BOUNDARY_KIND] = "host"
    if confirm_token is not None:
        decorated["confirm_token"] = confirm_token

    return ApprovalDecision(
        outcome=downstream.outcome,
        reason=downstream.reason,
        metadata=decorated,
    )


__all__ = ["ConsentResolver"]
