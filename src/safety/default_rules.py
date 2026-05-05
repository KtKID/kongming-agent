"""safety v0.1.4 — 默认三张规则表数据。

本文件提供 ``hard_deny_commands`` / ``approval_required_commands`` /
``sensitive_paths`` 的内嵌默认数据。用户 yaml 配置以 **追加**（不覆盖）
方式扩展，确保安全基线不可被降级。

严格遵守 ``docs/safety-scope-v0.1.4/04-data-and-state.md`` § 配置 schema 示例。
本文件仅承载数据，不做任何运行时判定 / 路径解析 / 命令匹配。
"""

from __future__ import annotations

from safety.types import (
    ApprovalRequiredCommand,
    BoundaryScope,
    HardDenyCommand,
    SensitivePathRule,
    SkillCallRule,
)

# ---------------------------------------------------------------------------
# hard_deny_commands —— 命中即拒绝，不进 ask
# ---------------------------------------------------------------------------

DEFAULT_HARD_DENY_COMMANDS: tuple[HardDenyCommand, ...] = (
    HardDenyCommand(
        # 仅匹配 rm -r* 后跟独立的根级目标：/, ~, $HOME, ., /*
        # 子目录如 rm -rf /home 由 sensitive_paths 规则处理；
        # rm -rf ~/Documents 由 boundary/sensitive_paths 处理。
        # 限定 segment 起止确保不误命中 `python rm.py` 这种文本。
        name="host-root-delete",
        matcher=r"^rm\s+(-[a-zA-Z]*r[a-zA-Z]*|-r|--recursive)\b(\s+--no-preserve-root)?\s+(/|~|\$HOME|\.|/\*)\s*$",
        match_mode="segment_regex",
        boundary_scope=BoundaryScope.HOST,
        reason="删除宿主根目录或主目录",
    ),
    HardDenyCommand(
        # 使用 (?<!\S) 而非 \b：`-` 不是 word 字符，\b 在空格+-边界处不成立
        name="rm-rf-no-preserve-root",
        matcher=r"(?<!\S)--no-preserve-root(?!\S)",
        match_mode="segment_regex",
        boundary_scope=BoundaryScope.ANY,
        reason="禁用根目录保护：无论目标是何路径都拒绝",
    ),
    HardDenyCommand(
        name="raw-disk-write",
        matcher=r"\bdd\s+.*of=/dev/(disk|sd|nvme|hd|mmc)",
        match_mode="segment_regex",
        boundary_scope=BoundaryScope.ANY,
        reason="写块设备",
    ),
    HardDenyCommand(
        name="mkfs-format",
        matcher=r"\bmkfs(\.\w+)?\b",
        match_mode="segment_regex",
        boundary_scope=BoundaryScope.ANY,
        reason="格式化文件系统",
    ),
    HardDenyCommand(
        name="fdisk-write",
        matcher=r"\bfdisk\s+/dev/",
        match_mode="segment_regex",
        boundary_scope=BoundaryScope.HOST,
        reason="分区表写入",
    ),
    HardDenyCommand(
        name="chmod-zero-root",
        matcher=r"\bchmod\s+(-R\s+)?0+\s+/(\s|$)",
        match_mode="segment_regex",
        boundary_scope=BoundaryScope.HOST,
        reason="把根目录权限置零",
    ),
    HardDenyCommand(
        # 覆盖 chmod -R 0 /etc, /usr, /bin, /sbin, /var, /root, /home, /boot 等系统目录
        name="chmod-zero-system-dirs",
        matcher=r"\bchmod\s+(-R\s+)?0+\s+/(etc|usr|bin|sbin|var|root|home|boot|lib|opt)(\s|/|$)",
        match_mode="segment_regex",
        boundary_scope=BoundaryScope.HOST,
        reason="递归归零关键系统目录权限",
    ),
    HardDenyCommand(
        # 经管道传入远程脚本执行：curl ... | sh 的接收方独立 segment 命中
        # 配合 segment 切分：第二段 sh / bash / zsh 命中即拒
        # 注意：直接执行本地脚本（如 sh script.sh）不走管道接收方语义，由其他规则覆盖
        name="pipe-to-shell",
        matcher=r"^\s*(sh|bash|zsh|dash|ksh)\s*(-[a-zA-Z]*)?\s*$",
        match_mode="segment_regex",
        boundary_scope=BoundaryScope.ANY,
        reason="执行经管道传入的远程脚本",
    ),
    HardDenyCommand(
        name="chown-recursive-root",
        matcher=r"\bchown\s+(-R\s+)?\S+\s+/(\s|$)",
        match_mode="segment_regex",
        boundary_scope=BoundaryScope.HOST,
        reason="递归改根目录所有者",
    ),
    HardDenyCommand(
        name="fork-bomb",
        matcher=r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        match_mode="segment_regex",
        boundary_scope=BoundaryScope.ANY,
        reason="fork bomb",
    ),
    HardDenyCommand(
        name="config-self-escalation",
        matcher="kongming_safety_config_escalate",
        match_mode="profile",
        boundary_scope=BoundaryScope.ANY,
        reason="试图扩大 agent 默认安全边界到根目录或 HOME",
    ),
)


# ---------------------------------------------------------------------------
# approval_required_commands —— 命中后由 severity 决定 ask 强度
# ---------------------------------------------------------------------------

DEFAULT_APPROVAL_REQUIRED_COMMANDS: tuple[ApprovalRequiredCommand, ...] = (
    ApprovalRequiredCommand(
        name="git-push",
        matcher="git_push",
        match_mode="profile",
        severity="standard",
        boundary_scope=BoundaryScope.ANY,
        reason="向远端发布变更",
    ),
    ApprovalRequiredCommand(
        name="git-reset-hard",
        matcher="git_reset_hard",
        match_mode="profile",
        severity="standard",
        boundary_scope=BoundaryScope.ANY,
        reason="不可逆地丢弃工作区改动",
    ),
    ApprovalRequiredCommand(
        name="workspace-external-write",
        matcher="workspace_external_write",
        match_mode="profile",
        severity="standard",
        boundary_scope=BoundaryScope.ANY,
        reason="写入工作区外路径",
    ),
    ApprovalRequiredCommand(
        name="config-boundary-edit",
        matcher="kongming_config_write",
        match_mode="profile",
        severity="elevated",
        boundary_scope=BoundaryScope.ANY,
        reason="修改 agent 安全边界配置",
    ),
    ApprovalRequiredCommand(
        name="agents-md-edit",
        matcher="agents_md_write",
        match_mode="profile",
        severity="elevated",
        boundary_scope=BoundaryScope.ANY,
        reason="修改 AGENTS.md / CLAUDE.md 行为约束",
    ),
    ApprovalRequiredCommand(
        name="pyproject-safety-section-edit",
        matcher="pyproject_safety_section_write",
        match_mode="profile",
        severity="elevated",
        boundary_scope=BoundaryScope.ANY,
        reason="修改 pyproject.toml [tool.kongming.safety] 段",
    ),
)


# ---------------------------------------------------------------------------
# sensitive_paths —— 路径风险按 ops 区分 read/write
# ---------------------------------------------------------------------------

DEFAULT_SENSITIVE_PATHS: tuple[SensitivePathRule, ...] = (
    # block：凭据与系统接口
    SensitivePathRule(
        name="ssh-material",
        matcher="~/.ssh/",
        match_mode="path_prefix",
        ops=frozenset({"read", "write"}),
        effect="block",
        boundary_scope=BoundaryScope.HOST,
        reason="SSH 凭据与主机信任关系",
    ),
    SensitivePathRule(
        name="gnupg-material",
        matcher="~/.gnupg/",
        match_mode="path_prefix",
        ops=frozenset({"read", "write"}),
        effect="block",
        boundary_scope=BoundaryScope.HOST,
        reason="GnuPG 私钥",
    ),
    SensitivePathRule(
        name="aws-credentials",
        matcher="~/.aws/credentials",
        match_mode="path_prefix",
        ops=frozenset({"read", "write"}),
        effect="block",
        boundary_scope=BoundaryScope.HOST,
        reason="AWS 凭据",
    ),
    SensitivePathRule(
        name="netrc",
        matcher="~/.netrc",
        match_mode="path_prefix",
        ops=frozenset({"read", "write"}),
        effect="block",
        boundary_scope=BoundaryScope.HOST,
        reason="网络资源凭据",
    ),
    SensitivePathRule(
        name="docker-config",
        matcher="~/.docker/config.json",
        match_mode="path_prefix",
        ops=frozenset({"read", "write"}),
        effect="block",
        boundary_scope=BoundaryScope.HOST,
        reason="Docker registry 凭据",
    ),
    SensitivePathRule(
        name="kongming-global-config",
        matcher="~/.kongming/",
        match_mode="path_prefix",
        ops=frozenset({"read", "write"}),
        effect="block",
        boundary_scope=BoundaryScope.HOST,
        reason="agent 全局配置（用户级，不允许 agent 改）",
    ),
    SensitivePathRule(
        name="git-objects",
        matcher=".git/objects/",
        match_mode="project_relative",
        ops=frozenset({"write"}),
        effect="block",
        boundary_scope=BoundaryScope.ANY,
        reason="VCS 内部对象，写入会破坏仓库完整性",
    ),
    SensitivePathRule(
        name="git-refs",
        matcher=".git/refs/",
        match_mode="project_relative",
        ops=frozenset({"write"}),
        effect="block",
        boundary_scope=BoundaryScope.ANY,
        reason="VCS refs，写入会破坏仓库",
    ),
    # elevated：项目敏感文件
    SensitivePathRule(
        name="aws-config",
        matcher="~/.aws/config",
        match_mode="path_prefix",
        ops=frozenset({"read", "write"}),
        effect="elevated",
        boundary_scope=BoundaryScope.HOST,
        reason="AWS 非凭据配置",
    ),
    SensitivePathRule(
        name="project-env",
        matcher=".env*",
        match_mode="glob",
        ops=frozenset({"read", "write"}),
        effect="elevated",
        boundary_scope=BoundaryScope.ANY,
        reason="项目密钥与运行时配置",
    ),
    SensitivePathRule(
        name="agents-md",
        matcher="AGENTS.md",
        match_mode="project_relative",
        ops=frozenset({"write"}),
        effect="elevated",
        boundary_scope=BoundaryScope.ANY,
        reason="agent 行为约束源",
    ),
    SensitivePathRule(
        name="claude-md",
        matcher="CLAUDE.md",
        match_mode="project_relative",
        ops=frozenset({"write"}),
        effect="elevated",
        boundary_scope=BoundaryScope.ANY,
        reason="agent 行为约束源",
    ),
    SensitivePathRule(
        name="kongming-config",
        matcher=".kongming/config",
        match_mode="project_relative",
        ops=frozenset({"write"}),
        effect="elevated",
        boundary_scope=BoundaryScope.ANY,
        reason="项目级 agent 配置",
    ),
    SensitivePathRule(
        name="kongming-safety-dir",
        matcher=".kongming/safety/",
        match_mode="project_relative",
        ops=frozenset({"write"}),
        effect="elevated",
        boundary_scope=BoundaryScope.ANY,
        reason="项目级安全规则目录",
    ),
    # allow：低风险 scratch 目录
    SensitivePathRule(
        name="kongming-workdir",
        matcher=".kongming/work/",
        match_mode="project_relative",
        ops=frozenset({"read", "write"}),
        effect="allow",
        boundary_scope=BoundaryScope.ANY,
        reason="低风险工作目录",
    ),
    SensitivePathRule(
        name="kongming-tmpdir",
        matcher=".kongming/tmp/",
        match_mode="project_relative",
        ops=frozenset({"read", "write"}),
        effect="allow",
        boundary_scope=BoundaryScope.ANY,
        reason="低风险临时目录",
    ),
)


# ---------------------------------------------------------------------------
# skill_call_rules —— skill 调用规则（v0.1.6 skill-system 模块 C 引入）
# ---------------------------------------------------------------------------

DEFAULT_SKILL_CALL_RULES: tuple[SkillCallRule, ...] = ()
"""默认空 → 等价 deny-by-default：未命中规则即走默认 standard ask 分流。

用户在 ``setting.yaml`` 的 ``safety.skill_call_rules`` 显式追加 allow / elevated /
block 规则，与现有三表并列。
"""


# ---------------------------------------------------------------------------
# trusted_workdirs / allow_writes / allow_tools_silent 默认空集
# ---------------------------------------------------------------------------

DEFAULT_TRUSTED_WORKDIRS: tuple[str, ...] = (
    ".kongming/work/",
    ".kongming/tmp/",
)

DEFAULT_ALLOW_WRITES: tuple[str, ...] = ()
"""默认空：用户必须显式声明扩展工作区外的 silent write 路径。"""

DEFAULT_ALLOW_TOOLS_SILENT: tuple[str, ...] = (
    "read_file",
    "list_dir",
)


# ---------------------------------------------------------------------------
# consent-then-trust 工具清单 —— 文档型常量（v0.1.6 memory / v0.2 schedule）
# ---------------------------------------------------------------------------
#
# 列出这些 tool 默认走"首次 consent → 后续 trust"模式：
#
# 1. ConsentResolver._classify 没有为这些 tool_name 写专用分支 → 命中
#    fallback ``unknown-tool-default`` → severity="standard" → 调
#    InteractiveApproval 拿用户决策。
# 2. 用户点击"本 session 同意"后，:class:`SafetyGatedApproval` 写
#    ``Grant(capability=f"tool:{tool_name}", matcher="*", scope=session)``。
# 3. 同 session 后续调用：:class:`TrustResolver` 的 ``tool:<name>`` 通配查询
#    （``trust.py`` ``_lookup_grant`` 第 2 步）命中 → 返回
#    ``decision_class=silent_allow`` + ``decision_source=session``。
#
# 本常量**不被任何 guard 直接消费** —— 该语义由 ConsentResolver fallback +
# SafetyGatedApproval._maybe_write_session_grant + TrustResolver 通配三件
# 套合作实现。维护此清单是为了：
#
# - 给 import-linter / 装配审计提供"哪些工具确实走这条 lane"的真源
# - 让新增此类工具时（如 v0.2 ``schedule``）有一处可登记的锚点
# - 测试通过 ``DEFAULT_CONSENT_THEN_TRUST_TOOLS`` 锁定行为不被无声破坏
#
# 注：``read_file`` / ``list_dir`` 不在此清单——它们走 ``DEFAULT_ALLOW_TOOLS_SILENT``
# intrinsic 静默放行。``write_file`` / ``run_shell`` 不在此清单——它们各有专用
# severity 分流（sensitive_paths / approval_required_commands）。

DEFAULT_CONSENT_THEN_TRUST_TOOLS: tuple[str, ...] = (
    "memory",
    "schedule",
)
"""默认走 "首次 standard consent → 后续 session-scoped trust" 模式的工具名集合。

每个 tool_name 对应 capability ``tool:<tool_name>``：

- ``memory``：v0.1.3 引入；MemoryTool 写入需用户首次确认，授予后本 session 内静默。
- ``schedule``：v0.2 引入（agent-cron-module-v0.2 模块 6）；ScheduleTool 创建 / 暂停 /
  删除 / run_now 等 action 走同一 capability，首次确认后本 session 静默。
  ``cron run`` 内 ``schedule_tool`` 会被装配层裁掉，这里的规则在 cron run 上下文
  不会被命中——属于双重保护中的 default-rule 一侧。

新增此类工具时在此追加 tool_name 即可；不需要改 ConsentResolver / TrustResolver。
"""


__all__ = [
    "DEFAULT_ALLOW_TOOLS_SILENT",
    "DEFAULT_ALLOW_WRITES",
    "DEFAULT_APPROVAL_REQUIRED_COMMANDS",
    "DEFAULT_CONSENT_THEN_TRUST_TOOLS",
    "DEFAULT_HARD_DENY_COMMANDS",
    "DEFAULT_SENSITIVE_PATHS",
    "DEFAULT_SKILL_CALL_RULES",
    "DEFAULT_TRUSTED_WORKDIRS",
]
