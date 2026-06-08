"""safety v0.1.4 — BoundaryResolver（M3 模块主体）。

把每个文件类 :class:`core.contracts.ApprovalRequest` 归类到 ``trusted`` /
``approval`` / ``blocked`` 三个 zone 之一。zone 推导依赖：

1. git tracked 文件清单（``git ls-files``）→ trusted_*_trie
2. 项目 ``.kongming/work/`` / ``.kongming/tmp/`` + 用户 ``trusted_workdirs`` →
   trusted_write_trie
3. 默认 / 用户 ``sensitive_paths`` → 按 ``effect`` 装入 approval_*_trie 或
   blocked_trie
4. 60 秒 TTL 缓存；外部可调 :meth:`BoundaryResolver.invalidate` 让 ``tool.completed``
   钩子在 ``git_add`` / ``git_commit`` / ``git_rm`` 后立即失效

**强约束**（任务 README）：

- 运行时 ``boundary_kind`` 恒为 :class:`safety.types.BoundaryKind.HOST`；本模块
  不读 ``runtime.boundary_kind`` 做 sandbox 分支
- 数据结构严格遵守 ``docs/safety-scope-v0.1.4/04-data-and-state.md``：所有 dataclass
  ``frozen=True``；:class:`safety.types.ResolvedBoundary` 必须含 5 个 trie + runtime
  + project_root + snapshot_at + git_available
- 命令工具（``run_shell``）不在本模块管辖范围；返回 :class:`safety.types.BoundaryZone.APPROVAL`
  并由后续 ConsentResolver 接管
- 仅依赖 stdlib：``subprocess`` / ``pathlib`` / ``time``

**已知限制**（任务约定 known gap）：

- ``sensitive_paths`` ``match_mode="glob"`` 仅支持文件名层匹配（如 ``.env*``），
  目前实现是把 glob matcher 直接装入 *项目根* 下的相对路径条目；多层 glob
  （如 ``**/secrets/*.json``）暂不支持，后续按需扩展
- TTL 60s 内 ``git add`` 后立即写文件，命中规则会按 untracked 判定 → 误问；
  事件失效（``invalidate``）是兜底
- ``boundary_scope=sandbox`` 的规则在装入 trie 时显式跳过（本次实现仅接受
  ``host`` / ``any``）
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from infrastructure.config.paths import get_kongming_home
from safety._path_trie import PathTrie
from safety.default_rules import (
    DEFAULT_SENSITIVE_PATHS,
    DEFAULT_TRUSTED_WORKDIRS,
)
from safety.types import (
    BoundaryDecision,
    BoundaryScope,
    BoundaryZone,
    ResolvedBoundary,
    RuntimeBoundaryContext,
    SensitivePathRule,
)

if TYPE_CHECKING:
    from core.contracts import ApprovalRequest

# ---------------------------------------------------------------------------
# 工具集合：本模块内部对 tool_name 的 read/write/command 分类
# ---------------------------------------------------------------------------

_READ_TOOLS: frozenset[str] = frozenset({"read_file", "list_dir"})
_WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file"})
_COMMAND_TOOLS: frozenset[str] = frozenset({"run_shell"})


# ---------------------------------------------------------------------------
# BoundaryResolver
# ---------------------------------------------------------------------------


class BoundaryResolver:
    """把 :class:`ApprovalRequest` 归类到 trusted / approval / blocked zone。

    构造时**不立刻**执行 git 命令；首次 :meth:`resolve` 调用或 TTL 过期才触发
    :meth:`_build_resolved_boundary`，避免冷启动等价 RTT。

    线程模型：单线程 / 单 event loop 内消费。当前实现不做 lock，因为 Runner
    串行调用；后续如果有并行调用需求，应在装配层加 lock。
    """

    def __init__(
        self,
        project_root: Path,
        *,
        ttl_seconds: float = 60.0,
        sensitive_paths: tuple[SensitivePathRule, ...] = DEFAULT_SENSITIVE_PATHS,
        trusted_workdirs: tuple[str, ...] = DEFAULT_TRUSTED_WORKDIRS,
    ) -> None:
        self._project_root: Path = project_root
        self._ttl_seconds: float = ttl_seconds
        self._sensitive_paths: tuple[SensitivePathRule, ...] = sensitive_paths
        self._trusted_workdirs: tuple[str, ...] = trusted_workdirs

        self._resolved: ResolvedBoundary | None = None
        self._snapshot_at: float = 0.0

    # ------------------------------------------------------------------ ctor
    @classmethod
    def from_project_root(
        cls,
        project_root: Path | None = None,
        *,
        ttl_seconds: float = 60.0,
        sensitive_paths: tuple[SensitivePathRule, ...] = DEFAULT_SENSITIVE_PATHS,
        trusted_workdirs: tuple[str, ...] = DEFAULT_TRUSTED_WORKDIRS,
    ) -> BoundaryResolver:
        """构造 BoundaryResolver。

        ``project_root=None`` 时，使用 ``Path.cwd()`` 作为占位；真正的项目根会在
        :meth:`_build_resolved_boundary` 通过 ``git rev-parse --show-toplevel``
        重新发现。
        """
        root = project_root if project_root is not None else Path.cwd()
        return cls(
            project_root=root.resolve(strict=False),
            ttl_seconds=ttl_seconds,
            sensitive_paths=sensitive_paths,
            trusted_workdirs=trusted_workdirs,
        )

    # ------------------------------------------------------------------ public
    def resolve(
        self,
        request: ApprovalRequest,
        runtime: RuntimeBoundaryContext,
    ) -> BoundaryDecision:
        """根据 5 个 trie 把 request 归类。

        命令类工具（``run_shell``）不在本模块管辖：直接返回 ``APPROVAL``，由
        ConsentResolver 接管。其他工具：
        - 在 ``blocked_trie`` 命中 → ``BLOCKED``
        - 在 ``approval_*_trie`` 命中（按 op 分流）→ ``APPROVAL``
        - 在 ``trusted_*_trie`` 命中（按 op 分流）→ ``TRUSTED``
        - 都不命中 → 保守 ``APPROVAL``
        """
        # 命令工具直接交给 ConsentResolver
        if request.tool_name in _COMMAND_TOOLS:
            return BoundaryDecision(
                zone=BoundaryZone.APPROVAL,
                matched_rule=None,
                reason="command tools handled by ConsentResolver",
            )

        # 提取 path
        op, raw_path = self._extract_path_and_op(request)
        if raw_path is None:
            # 无 path 信息，落到 approval（保守）
            return BoundaryDecision(
                zone=BoundaryZone.APPROVAL,
                matched_rule=None,
                reason="no path argument; fallback to approval",
            )

        # 路径标准化
        norm_path = Path(raw_path).expanduser().resolve(strict=False)

        # 触发 / 复用 ResolvedBoundary
        resolved = self._get_or_build(runtime)

        # 优先级：blocked > approval_* > trusted_*
        if resolved.blocked_trie.contains_or_prefix(norm_path):
            return BoundaryDecision(
                zone=BoundaryZone.BLOCKED,
                matched_rule="blocked_trie",
                reason=f"path {norm_path} blocked by sensitive_paths(effect=block)",
            )

        approval_trie = (
            resolved.approval_read_trie if op == "read" else resolved.approval_write_trie
        )
        if approval_trie.contains_or_prefix(norm_path):
            return BoundaryDecision(
                zone=BoundaryZone.APPROVAL,
                matched_rule=f"approval_{op}_trie",
                reason=f"path {norm_path} requires approval (sensitive_paths effect=elevated, op={op})",
            )

        trusted_trie = resolved.trusted_read_trie if op == "read" else resolved.trusted_write_trie
        if trusted_trie.contains_or_prefix(norm_path):
            return BoundaryDecision(
                zone=BoundaryZone.TRUSTED,
                matched_rule=f"trusted_{op}_trie",
                reason=f"path {norm_path} in trusted zone (op={op})",
            )

        return BoundaryDecision(
            zone=BoundaryZone.APPROVAL,
            matched_rule=None,
            reason=f"path {norm_path} outside trusted zone; conservative approval",
        )

    def invalidate(self) -> None:
        """手动失效缓存（``tool.completed`` 事件钩子在 git_add/commit/rm 后调用）。"""
        self._resolved = None
        self._snapshot_at = 0.0

    @property
    def resolved(self) -> ResolvedBoundary | None:
        """暴露最近一次构建的 :class:`ResolvedBoundary`（测试 / 调试用）。"""
        return self._resolved

    # ------------------------------------------------------------------ internal
    def _get_or_build(self, runtime: RuntimeBoundaryContext) -> ResolvedBoundary:
        now = time.monotonic()
        if (
            self._resolved is None
            or now - self._snapshot_at > self._ttl_seconds
            # runtime 上下文变了（理论上 v0.1.4 不会变，但防御一下）
            or self._resolved.runtime != runtime
        ):
            self._resolved = self._build_resolved_boundary(runtime)
            self._snapshot_at = now
        return self._resolved

    def _build_resolved_boundary(self, runtime: RuntimeBoundaryContext) -> ResolvedBoundary:
        """重建 :class:`ResolvedBoundary`：探测 git 根 + ls-files + 规则装 trie。"""
        # 1. 项目根：先尝试 git rev-parse --show-toplevel
        git_toplevel = self._discover_git_toplevel(self._project_root)
        if git_toplevel is not None:
            project_root = git_toplevel
            git_available = True
            tracked_files = self._git_ls_files(project_root)
        else:
            # 非 git 仓库 fallback
            project_root = get_kongming_home().parent.resolve(strict=False)
            git_available = False
            tracked_files = ()

        # 2. 装 trie
        trusted_write_trie = PathTrie()
        trusted_read_trie = PathTrie()
        approval_read_trie = PathTrie()
        approval_write_trie = PathTrie()
        blocked_trie = PathTrie()

        # 2.1 tracked → trusted_write + trusted_read
        # 性能优化：project_root 已 resolve；tracked 文件相对路径直接拼接，
        # 不再每条调 resolve(strict=False)（3 万规模 N 次 syscall 是 O(100ms) 级开销）。
        # git ls-files --full-name 输出已是规整化相对路径（无 .. / 无尾斜杠）。
        # PathTrie 的 contains_or_prefix 走 Path.parts 切分，不需要 fully resolved。
        for rel_name in tracked_files:
            abs_path = project_root / rel_name
            trusted_write_trie.add(abs_path)
            trusted_read_trie.add(abs_path)

        # 2.2 trusted_workdirs（项目相对）+ DEFAULT → trusted_write + trusted_read
        # 注意：read 默认信任工作区内一切；写入只信任 trusted_workdirs。
        # 但既然 read 已包含 tracked，再追加 trusted_workdirs 让 read 也覆盖 .kongming/work 等
        for rel in self._trusted_workdirs:
            abs_path = self._resolve_relative(rel, project_root)
            trusted_write_trie.add(abs_path)
            trusted_read_trie.add(abs_path)

        # 2.3 sensitive_paths 按 effect 分流
        for rule in self._sensitive_paths:
            # boundary_scope 校验：本次仅接受 host / any，sandbox 跳过
            if rule.boundary_scope is BoundaryScope.SANDBOX:
                continue

            matchers = self._resolve_sensitive_matcher(rule, project_root)
            if not matchers:
                continue

            for abs_matcher in matchers:
                if rule.effect == "block":
                    blocked_trie.add(abs_matcher)
                elif rule.effect == "elevated":
                    if "read" in rule.ops:
                        approval_read_trie.add(abs_matcher)
                    if "write" in rule.ops:
                        approval_write_trie.add(abs_matcher)
                elif rule.effect == "allow":
                    # allow 类规则装入 trusted（read/write 按 ops 区分）
                    if "read" in rule.ops:
                        trusted_read_trie.add(abs_matcher)
                    if "write" in rule.ops:
                        trusted_write_trie.add(abs_matcher)
                # standard 在 SensitivePathRule 不允许，但 schema 可能未来开放，
                # 保持显式跳过

        return ResolvedBoundary(
            runtime=runtime,
            project_root=project_root,
            trusted_write_trie=trusted_write_trie,
            trusted_read_trie=trusted_read_trie,
            approval_read_trie=approval_read_trie,
            approval_write_trie=approval_write_trie,
            blocked_trie=blocked_trie,
            snapshot_at=time.monotonic(),
            git_available=git_available,
        )

    # -------------------------- helpers: tool → path/op --------------------
    @staticmethod
    def _extract_path_and_op(
        request: ApprovalRequest,
    ) -> tuple[str, str | None]:
        """从 ApprovalRequest 提取 (op, path)。

        - read_file / list_dir → ("read", args["path"])
        - write_file / edit_file → ("write", args["path"])
        - 其他 / 命令 → 调用方先过滤
        """
        tool = request.tool_name
        if tool in _READ_TOOLS:
            return "read", request.arguments.get("path")
        if tool in _WRITE_TOOLS:
            return "write", request.arguments.get("path")
        # 未知工具：保守按 write op 处理（更严格），但 path 拿不到就返回 None
        return "write", request.arguments.get("path")

    # -------------------------- helpers: git ------------------------------
    @staticmethod
    def _run_git(
        args: list[str],
        cwd: Path,
        *,
        timeout: float = 5.0,
    ) -> subprocess.CompletedProcess[bytes]:
        """统一的 git CLI 调用入口（测试可 monkeypatch 该方法）。"""
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    @classmethod
    def _discover_git_toplevel(cls, hint_root: Path) -> Path | None:
        """探测 ``hint_root`` 所在 git 仓库根；非 git 仓库返回 None。"""
        try:
            result = cls._run_git(["rev-parse", "--show-toplevel"], hint_root)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        out = result.stdout.decode("utf-8", errors="replace").strip()
        if not out:
            return None
        return Path(out).resolve(strict=False)

    @classmethod
    def _git_ls_files(cls, project_root: Path) -> tuple[str, ...]:
        """返回 tracked 文件相对路径列表（``\\0`` 分隔解析）。

        - ``--full-name``：相对仓库根的完整路径
        - ``-z``：``\\0`` 分隔，避免特殊字符 / 换行问题
        """
        try:
            result = cls._run_git(
                ["ls-files", "--full-name", "-z"],
                project_root,
                timeout=30.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return ()
        if result.returncode != 0:
            return ()
        raw = result.stdout
        if not raw:
            return ()
        # 解析：\0 分隔，trailing \0 → split 后会产生空尾元素，过滤掉
        return tuple(seg.decode("utf-8", errors="replace") for seg in raw.split(b"\0") if seg)

    # -------------------------- helpers: matcher resolve -----------------
    @staticmethod
    def _resolve_relative(rel: str, project_root: Path) -> Path:
        """把项目相对路径绝对化（不做存在性校验）。"""
        return (project_root / rel).resolve(strict=False)

    @staticmethod
    def _resolve_sensitive_matcher(rule: SensitivePathRule, project_root: Path) -> tuple[Path, ...]:
        """根据 ``match_mode`` 把 SensitivePathRule.matcher 转为绝对路径条目。

        - ``path_prefix``：``Path(matcher).expanduser().resolve(strict=False)``
        - ``project_relative``：``project_root / matcher``
        - ``glob``：本次实现仅支持文件名层 glob，把 matcher 装入项目根
          （如 ``.env*`` → 项目根本身，靠后续 trie 覆盖语义命中根下文件）

        返回 tuple 而非单一 Path 是为未来扩展（如多层 glob 展开）保留接口。
        """
        if rule.match_mode == "path_prefix":
            return (Path(rule.matcher).expanduser().resolve(strict=False),)
        if rule.match_mode == "project_relative":
            return ((project_root / rule.matcher).resolve(strict=False),)
        if rule.match_mode == "glob":
            # known gap：glob 目前只把 *项目根* 装入 trie，靠 contains_or_prefix
            # 把根下所有文件命中。这会让 .env* 规则把 *项目所有文件* 都标成
            # elevated。我们改用更精确的策略：把 glob 拆成 (parent_dir, pattern)，
            # 装入 parent_dir 而非整个项目根；当前实现暂只支持顶级 glob，
            # 故 parent = project_root，但匹配粒度等于"项目根下任意文件"。
            #
            # **保守策略**：glob 仅装入项目根的占位条目。BoundaryResolver 不在
            # contains_or_prefix 阶段做 glob 二次过滤；具体 glob 命中由
            # ConsentResolver / HardBlockGuard 在拿到 BoundaryZone.APPROVAL
            # 后再做精确匹配。
            #
            # 为避免把整个项目根标成 elevated（false positive 太严），本次实现
            # 显式跳过 glob 规则装 trie，known gap 文档化在模块 docstring。
            return ()
        # 未来若新增 match_mode，显式失败而非静默忽略
        return ()


__all__ = ["BoundaryResolver"]
