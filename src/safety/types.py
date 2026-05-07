"""safety v0.1.4 — 数据基座（schema 真源，零运行时判定）。

本文件严格遵守 ``docs/safety-scope-v0.1.4/04-data-and-state.md``：

- 所有 dataclass ``frozen=True``
- 所有枚举继承 ``str, Enum``
- 仅承载数据形态，不放任何匹配 / realpath / git 调用 / 决策逻辑
- ``boundary_scope`` / ``boundary_kind`` 是必填字段；运行时本次实现仅接受 ``host`` / ``any``

未来沙盒落地（v0.1.5+）：
- ``BoundaryKind.SANDBOX`` / ``BoundaryScope.SANDBOX`` 字段已预留
- 当前 guards 不读 ``boundary_kind`` 的 sandbox 分支
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from safety._path_trie import PathTrie


# ---------------------------------------------------------------------------
# 决策类别 / 证据来源 / 边界区域
# ---------------------------------------------------------------------------


class DecisionClass(StrEnum):
    """主决策链三段式分类。"""

    HARD_BLOCK = "hard_block"
    SILENT_ALLOW = "silent_allow"
    EXPLICIT_CONSENT = "explicit_consent"


class DecisionSource(StrEnum):
    """决策证据来源。

    ``intrinsic`` / ``session`` / ``config`` 用于 ``silent_allow``；
    ``standard`` / ``elevated`` 用于 ``explicit_consent`` 的审批强度。
    ``hard_block`` 不使用 source 字段。
    """

    INTRINSIC = "intrinsic"
    SESSION = "session"
    CONFIG = "config"
    STANDARD = "standard"
    ELEVATED = "elevated"


class BoundaryZone(StrEnum):
    """``BoundaryResolver.resolve`` 的归类结果。"""

    TRUSTED = "trusted"
    APPROVAL = "approval"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Runtime 边界类型（本次实现 host-only，sandbox 字段已预留）
# ---------------------------------------------------------------------------


class BoundaryKind(StrEnum):
    """运行时边界类型。

    v0.1.4 实现：所有 ``RuntimeBoundaryContext`` / ``GrantKey`` 实例
    必须为 ``HOST``；guards 不写 sandbox 分支判定。
    """

    HOST = "host"
    SANDBOX = "sandbox"


class BoundaryScope(StrEnum):
    """规则适用的边界范围。

    yaml 配置加载时仅接受 ``ANY`` / ``HOST``；命中 ``SANDBOX`` 直接拒绝。
    """

    ANY = "any"
    HOST = "host"
    SANDBOX = "sandbox"


# ---------------------------------------------------------------------------
# SafetyRule（统一抽象）— 三张规则表的共同视图
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SafetyRule:
    """统一表达路径和命令规则。

    本类是高层概念抽象，便于跨规则表生成同一份 trace 字段；
    具体规则表使用各自专用 dataclass（见下方）。
    """

    name: str
    target: Literal["path", "command", "toml_section"]
    matcher: str
    ops: frozenset[Literal["read", "write", "exec"]]
    effect: Literal["block", "standard", "elevated", "allow"]
    reason: str


# ---------------------------------------------------------------------------
# 三张规则表的数据形态
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HardDenyCommand:
    """``hard_deny_commands`` 表条目：命中即 ``decision_class=hard_block``。"""

    name: str
    matcher: str  # regex 或 action profile id
    match_mode: Literal["segment_regex", "profile"]
    boundary_scope: BoundaryScope
    reason: str


@dataclass(frozen=True)
class ApprovalRequiredCommand:
    """``approval_required_commands`` 表条目：命中后由 ``severity`` 决定 ask 强度。"""

    name: str
    matcher: str
    match_mode: Literal["segment_regex", "profile"]
    severity: Literal["standard", "elevated"]
    boundary_scope: BoundaryScope
    reason: str


@dataclass(frozen=True)
class SensitivePathRule:
    """``sensitive_paths`` 表条目：路径风险按 ``ops`` 区分 read/write。"""

    name: str
    matcher: str  # absolute prefix, project-relative path, glob
    match_mode: Literal["path_prefix", "project_relative", "glob"]
    ops: frozenset[Literal["read", "write", "exec"]]
    effect: Literal["block", "elevated", "allow"]
    boundary_scope: BoundaryScope
    reason: str


@dataclass(frozen=True)
class DestructivePattern:
    """``destructive_always_ask`` 表条目：命中即强制 consent，无视任何 grant。

    决策链优先级第 ② 层（HardBlock → **DestructiveForceAsk** → Trust → Consent）。
    复用 ``HardDenyCommand`` 的字段结构，但语义不同：
    - HardDenyCommand 命中 → ``rejected``（硬拒绝，不可绕过）
    - DestructivePattern 命中 → 跳过 Trust 直接进 ConsentResolver（强制 elevated 审批）

    匹配逻辑：对命令按 shell 分隔符（``&&`` / ``||`` / ``;`` / ``|``）拆段，
    每段独立用 ``re.search`` 检查。
    """

    name: str
    matcher: str  # regex
    match_mode: Literal["segment_regex"]
    boundary_scope: BoundaryScope
    reason: str


@dataclass(frozen=True)
class SkillCallRule:
    """``skill_call_rules`` 表条目：skill 调用按 ``effect`` 区分 block / elevated / allow。

    v0.1.6 skill-system 模块 C 引入。与 ``ApprovalRequiredCommand`` /
    ``SensitivePathRule`` 三表并列；由 :class:`safety.guards.consent.ConsentResolver`
    在 ``tool_name == "skill"`` 时消费。

    Attributes:
        name: 规则唯一标识。
        matcher: skill name 文本（exact 模式）或前缀（prefix 模式）。
        match_mode: ``"exact"`` 全字匹配 / ``"prefix"`` 前缀匹配。
        effect: ``"block"`` 拒绝调用 / ``"elevated"`` 升级 ask / ``"allow"`` 静默放行。
        boundary_scope: 规则适用的边界范围；本次实现仅接受 ``ANY`` / ``HOST``。
        reason: 人读理由，写入 trace metadata.reason。
    """

    name: str
    matcher: str
    match_mode: Literal["exact", "prefix"]
    effect: Literal["block", "elevated", "allow"]
    boundary_scope: BoundaryScope
    reason: str


# ---------------------------------------------------------------------------
# BoundaryResolver 输出
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundaryDecision:
    """``BoundaryResolver.resolve`` 返回结构。"""

    zone: BoundaryZone
    matched_rule: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Grant 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrantKey:
    """grant 查询主键。

    ``boundary_kind`` 为必填字段，防止 sandbox grant 漏到 host。
    """

    capability: str
    matcher: str  # path prefix 或 action profile
    boundary_kind: BoundaryKind


@dataclass(frozen=True)
class Grant:
    """单条 grant 记录。

    - ``scope=session``：内存存储，session 结束失效；``session_id`` 必填，
      标记是哪个 session/thread 给出的授权（per-thread 物理隔离）。
    - ``scope=config``：yaml 持久化，config reload 失效；``session_id`` 必为
      ``None``（persist grant 跨 session 永久共享，不绑定单个 session）。

    数据结构层面强制 session 维度隔离，防止跨 thread 信任泄漏（参考
    ``reports/bug/bug-report-20260427-235232-grant-session-scoping.md``）。
    ``__post_init__`` 在构造时立即校验，写错立即抛 ``ValueError``。
    """

    key: GrantKey
    scope: Literal["session", "config"]
    source: DecisionSource
    created_at: float
    request_id: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.scope == "session" and self.session_id is None:
            raise ValueError(f"session grant requires session_id, got None (key={self.key!r})")
        if self.scope == "config" and self.session_id is not None:
            raise ValueError(
                "config (persist) grant must not carry session_id, "
                f"got {self.session_id!r} (key={self.key!r})"
            )


# ---------------------------------------------------------------------------
# Runtime 上下文
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeBoundaryContext:
    """每次审批请求必须携带的 runtime 上下文。

    v0.1.4 ``boundary_kind`` 恒为 ``BoundaryKind.HOST``，其余字段保留为接口预付。
    """

    boundary_kind: BoundaryKind
    runtime_root: Path | None = None
    sandbox_id: str | None = None
    network_policy: Literal["off", "allowlist", "full"] = "full"
    mounted_sensitive_paths: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# ResolvedBoundary（BoundaryResolver 缓存输出）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedBoundary:
    """``BoundaryResolver`` 的缓存形态。

    五个 trie 分别承载 read/write 在 trusted / approval / blocked 三个 zone 的归类。
    具体 ``PathTrie`` 实现在 ``safety/_path_trie.py``（M3）；本类仅作 schema。
    """

    runtime: RuntimeBoundaryContext
    project_root: Path
    trusted_write_trie: PathTrie
    trusted_read_trie: PathTrie
    approval_read_trie: PathTrie
    approval_write_trie: PathTrie
    blocked_trie: PathTrie
    snapshot_at: float
    git_available: bool


# ---------------------------------------------------------------------------
# Approval 元数据键约定（不改变 ApprovalDecision 协议形态，只约定 metadata 内的键）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalMetadataKeys:
    """``ApprovalDecision.metadata`` 在 v0.1.4 下的标准键名集合。

    v0.1.4 不改变 ``core.contracts.ApprovalDecision`` 协议形态，仅在 metadata 内
    约定 6 个新键 + 保留 ``stage`` 兼容字段。本类仅作常量集合，guards 通过
    ``ApprovalMetadataKeys.DECISION_CLASS`` 等访问，避免硬编码字符串散落各处。
    """

    DECISION_CLASS: str = "decision_class"
    DECISION_SOURCE: str = "decision_source"
    MATCHED_RULE: str = "matched_rule"
    REASON: str = "reason"
    GRANT_SCOPE: str = "grant_scope"
    BOUNDARY_KIND: str = "boundary_kind"
    STAGE: str = "stage"  # 兼容 v0.1.3 旧字段
    SUGGESTED_ALTERNATIVES: str = "suggested_alternatives"  # hard_block 时给 LLM 的建议


__all__ = [
    "ApprovalMetadataKeys",
    "ApprovalRequiredCommand",
    "BoundaryDecision",
    "BoundaryKind",
    "BoundaryScope",
    "BoundaryZone",
    "DecisionClass",
    "DecisionSource",
    "DestructivePattern",
    "Grant",
    "GrantKey",
    "HardDenyCommand",
    "ResolvedBoundary",
    "RuntimeBoundaryContext",
    "SafetyRule",
    "SensitivePathRule",
    "SkillCallRule",
]
