"""safety v0.1.4 M5.2 — GrantStore session/config 双层存储。

承载 ``Grant`` 的 session（内存）+ config（启动时从 yaml 读 / 通过
``_grant_persister`` 写回）双层语义：

- ``session`` grants：``dict[GrantKey, Grant]``，session 结束失效。
- ``config`` grants：启动时从 ``Config.safety.allow_writes`` /
  ``allow_tools_silent`` 转换为 :class:`Grant`；写回由模块 5.5 的
  :func:`safety.grants.persister.persist_grant` 完成（本类只标记）。

设计要点：

- :class:`GrantKey` 的 ``boundary_kind`` 在本类中**必须**为
  :attr:`BoundaryKind.HOST`；任何 sandbox grant 都被 ``put_*`` 方法显式拒绝。
  （v0.1.4 host-only；防 sandbox grant 漏到 host。）
- ``find_matching`` 实现 path-prefix 匹配语义：grant 的 matcher 是某目录前缀，
  请求路径以 grant.matcher 开头即命中。``"*"`` matcher 表示通配（用于
  ``allow_tools_silent`` 这类按 capability 整体放行的场景）。
- 短路顺序：先查 session 再查 config，session grant 可临时覆盖 config 默认。
- 模块 6 ``TrustResolver`` 是本 store 的主消费者，模块 4 / 5 协议联调里
  ``InteractiveApproval`` 选择 ``ACCEPT_FOR_SESSION`` / ``ACCEPT_PERSIST``
  时，由 chain 调用 ``put_session`` / ``put_persist``。

不依赖：

- 不直接 import ``ruamel.yaml`` / ``yaml``：写回 yaml 由
  ``_grant_persister.persist_grant`` 承担，本模块只做内存路由 + 状态标记。
- 不 import ``tools/`` / ``cli/``：避免反向依赖。
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import PurePosixPath

from infrastructure.config.models import Config
from safety.approval.types import (
    BoundaryKind,
    DecisionSource,
    Grant,
    GrantKey,
)

# ``allow_tools_silent`` 转 Grant 时的 capability 前缀。
# 命名约定：``tool:<tool_name>`` 把"按工具名静默放行"统一到 capability 维度。
_TOOL_CAPABILITY_PREFIX = "tool:"

# ``allow_writes`` 转 Grant 时的 capability 名。
# 与 file_tools.write_file / Edit / Patch 等写入类工具的 capability 对齐。
_FILE_WRITE_CAPABILITY = "file_write"

# Grant matcher 通配符：用于按 capability 整体放行（如 allow_tools_silent）。
_WILDCARD_MATCHER = "*"


class GrantStore:
    """session + config 双层 grant 存储。

    Attributes:
        _session_grants: 内存 session grants，key=GrantKey，value=Grant。
        _config_grants: 启动时从 yaml 加载的 config grants（dict 形态便于查询）。
        _pending_persist: 待持久化到 yaml 的 grant 列表（顺序保留，便于
            ``_grant_persister`` 批量回写）。
    """

    def __init__(
        self,
        *,
        config_grants: dict[GrantKey, Grant] | None = None,
    ) -> None:
        # session grants 按 session_id 分桶物理隔离（per-thread 隔离修复 P1 bug，
        # 见 reports/bug/bug-report-20260427-235232-grant-session-scoping.md）。
        # 外层 key=session_id；内层 dict 是该 session 自己的 grants。
        self._session_grants: dict[str, dict[GrantKey, Grant]] = {}
        self._config_grants: dict[GrantKey, Grant] = dict(config_grants or {})
        self._pending_persist: list[Grant] = []

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Config) -> GrantStore:
        """从 :class:`Config.safety` 加载 config grants。

        转换规则：

        - ``safety.allow_writes`` 中每条 path → ``GrantKey(capability="file_write",
          matcher=path, boundary_kind=HOST)``，``Grant.scope="config"``，
          ``source=DecisionSource.CONFIG``。
        - ``safety.allow_tools_silent`` 中每条 tool_name →
          ``GrantKey(capability=f"tool:{tool_name}", matcher="*",
          boundary_kind=HOST)``。
        """
        config_grants: dict[GrantKey, Grant] = {}
        now = time.time()

        for path in config.safety.allow_writes:
            key = GrantKey(
                capability=_FILE_WRITE_CAPABILITY,
                matcher=path,
                boundary_kind=BoundaryKind.HOST,
            )
            config_grants[key] = Grant(
                key=key,
                scope="config",
                source=DecisionSource.CONFIG,
                created_at=now,
                request_id=None,
            )

        for tool_name in config.safety.allow_tools_silent:
            key = GrantKey(
                capability=f"{_TOOL_CAPABILITY_PREFIX}{tool_name}",
                matcher=_WILDCARD_MATCHER,
                boundary_kind=BoundaryKind.HOST,
            )
            config_grants[key] = Grant(
                key=key,
                scope="config",
                source=DecisionSource.CONFIG,
                created_at=now,
                request_id=None,
            )

        return cls(config_grants=config_grants)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, key: GrantKey, *, session_id: str | None = None) -> Grant | None:
        """精确查询：先本 session 桶，再 config。

        ``key.boundary_kind != HOST`` 永远返回 ``None``：v0.1.4 host-only，
        sandbox grant 不应漏出（此处是兜底，put_* 已经拒绝过）。

        ``session_id`` 严格隔离：仅查询 ``session_id`` 命中的桶；``None`` 时
        跳过 session 桶（仅查 config）。这是 cross-session 信任泄漏的源头堵
        点（参考 ``bug-report-20260427-235232-grant-session-scoping.md``）。
        """
        if key.boundary_kind != BoundaryKind.HOST:
            return None
        if session_id is not None:
            bucket = self._session_grants.get(session_id)
            if bucket is not None and key in bucket:
                return bucket[key]
        return self._config_grants.get(key)

    def find_matching(
        self,
        capability: str,
        path_or_action: str,
        boundary_kind: BoundaryKind,
        *,
        session_id: str | None = None,
    ) -> Grant | None:
        """按 capability + path-prefix 语义查询。

        匹配规则（按短路顺序）：

        1. ``boundary_kind != HOST`` → 直接返回 ``None``。
        2. ``session_id`` 给定 → 先扫该 session 桶（capability 相等 +
           matcher 是 ``"*"`` 或 ``path_or_action`` 以 matcher 开头则命中）；
           ``None`` 时跳过 session 桶。
        3. session 未命中 → 同样规则扫 config grants（config 跨 session 共享）。

        命中多条时按"matcher 字符串更长更具体"原则取最长前缀；matcher 长度
        相同时按插入顺序（dict 默认顺序）。

        ``session_id`` 严格隔离：跨 session 不命中（修 P1 bug）。
        """
        if boundary_kind != BoundaryKind.HOST:
            return None

        if session_id is not None:
            bucket = self._session_grants.get(session_id, {})
            best = self._scan_dict(bucket, capability, path_or_action)
            if best is not None:
                return best
        return self._scan_dict(self._config_grants, capability, path_or_action)

    @staticmethod
    def _scan_dict(
        grants: dict[GrantKey, Grant],
        capability: str,
        path_or_action: str,
    ) -> Grant | None:
        """从一个 grants dict 里挑最具体的 path-prefix 命中项。"""
        best: Grant | None = None
        best_len = -1
        for key, grant in grants.items():
            if key.capability != capability:
                continue
            if key.matcher == _WILDCARD_MATCHER:
                # 通配 < 任意有具体 matcher 的命中
                if best_len < 0:
                    best = grant
                    best_len = 0
                continue
            if _path_prefix_match(path_or_action, key.matcher):
                length = len(key.matcher)
                if length > best_len:
                    best = grant
                    best_len = length
        return best

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def put_session(self, grant: Grant) -> None:
        """写入 session grant 到对应 session_id 的桶。

        强制条件：
        - ``grant.key.boundary_kind == HOST``：sandbox 漏到 host 的最后防线
        - ``grant.scope == "session"``：scope 校验
        - ``grant.session_id is not None``：session 桶 key（``Grant.__post_init__``
          已先校验过；这里是防御性二次校验）

        按 ``grant.session_id`` 找/建桶后写入。``clear_session(session_id)``
        是配套清理点。
        """
        self._validate_host_only(grant)
        if grant.scope != "session":
            raise ValueError(
                f"GrantStore.put_session requires grant.scope='session', got {grant.scope!r}"
            )
        if grant.session_id is None:
            # 防御性：Grant.__post_init__ 已校验，此处 mypy 收窄 + 双保险
            raise ValueError(
                f"GrantStore.put_session requires grant.session_id, got None (key={grant.key!r})"
            )
        bucket = self._session_grants.setdefault(grant.session_id, {})
        bucket[grant.key] = grant

    def put_persist(self, grant: Grant) -> None:
        """标记 grant 为待持久化。

        本方法只更新内存 ``_config_grants`` + 入队 ``_pending_persist``；
        真正写盘由 ``safety.grants.persister.persist_grant`` 承担（拆开是
        为了把 yaml IO 与 store 状态隔离，方便测试）。

        强制 ``grant.key.boundary_kind == HOST`` 与 ``grant.scope == "config"``。
        """
        self._validate_host_only(grant)
        if grant.scope != "config":
            raise ValueError(
                f"GrantStore.put_persist requires grant.scope='config', got {grant.scope!r}"
            )
        self._config_grants[grant.key] = grant
        self._pending_persist.append(grant)

    def clear_session(self, session_id: str) -> None:
        """清空指定 session 的所有 grants。

        web 多 thread 模式下 ``thread_manager.evict_cell`` 关闭某 thread 时
        必须调本方法清理对应 session 的 grants，避免长期运行 server 的内存
        泄漏。CLI 路径不需要调（进程退出时整个 GrantStore 跟着 GC 回收）。

        config grants 不动（跨 session 共享）。``session_id`` 不存在时静默
        跳过（幂等）。
        """
        self._session_grants.pop(session_id, None)

    # ------------------------------------------------------------------
    # 状态访问（持久化层 / 测试用）
    # ------------------------------------------------------------------

    def session_grants(self, session_id: str | None = None) -> tuple[Grant, ...]:
        """返回 session grants 的不可变快照（顺序稳定）。

        - ``session_id=None``：返回所有 session 的所有 grants（flatten）；
          供测试 / 总览用。
        - ``session_id="..."``：仅返回指定 session 桶里的 grants；供按
          session 审计用。
        """
        if session_id is not None:
            bucket = self._session_grants.get(session_id, {})
            return tuple(bucket.values())
        return tuple(grant for bucket in self._session_grants.values() for grant in bucket.values())

    def config_grants(self) -> tuple[Grant, ...]:
        """返回当前所有 config grants 的不可变快照。"""
        return tuple(self._config_grants.values())

    def take_pending_persist(self) -> tuple[Grant, ...]:
        """取走当前待持久化队列并清空。

        持久化层（``_grant_persister``）调用本方法批量写回 yaml；写完后
        队列被消费，不会重复落盘。
        """
        pending = tuple(self._pending_persist)
        self._pending_persist.clear()
        return pending

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_host_only(grant: Grant) -> None:
        if grant.key.boundary_kind != BoundaryKind.HOST:
            raise ValueError(
                "GrantStore v0.1.4 only accepts boundary_kind=HOST grants; "
                f"refused {grant.key.boundary_kind!r} (sandbox grant must not leak to host)."
            )


def _path_prefix_match(candidate: str, prefix: str) -> bool:
    """path-prefix 匹配：``candidate`` 在 ``prefix`` 树下。

    实现细节：

    - 普通字符串 startswith 即可，但要保证 ``"src/foo"`` 不会匹配
      ``"src/foobar"``（边界字符必须是路径分隔符或 prefix 的末尾就是分隔符）。
    - 不做 realpath：本类是查询层，realpath 由调用方在传入前完成
      （:class:`HardBlockGuard` / :class:`BoundaryResolver` 已经处理）。
    """
    if not prefix:
        return False
    if candidate == prefix:
        return True
    # PurePosixPath 在跨平台测试更稳；safety 内部统一用 posix 风格 path。
    if prefix.endswith("/"):
        return candidate.startswith(prefix)
    # ``prefix`` 不带尾斜杠 → 要求 candidate 比 prefix 多一个 "/" 分隔符
    return candidate.startswith(prefix + "/") or candidate == prefix


def grant_with_now(
    *,
    capability: str,
    matcher: str,
    scope: str,
    source: DecisionSource,
    request_id: str | None = None,
    session_id: str | None = None,
) -> Grant:
    """构造一条 ``boundary_kind=HOST`` 的新 Grant（``created_at=now``）。

    helper：让 chain / resolver 在做 put_session / put_persist 时不重复写
    时间戳与 boundary_kind。

    ``session_id``：scope=session 时必传；scope=config 时必为 ``None``。
    （Grant.__post_init__ 会校验，传错立即抛 ValueError。）
    """
    if scope not in ("session", "config"):
        raise ValueError(f"grant scope must be 'session' or 'config', got {scope!r}")
    key = GrantKey(
        capability=capability,
        matcher=matcher,
        boundary_kind=BoundaryKind.HOST,
    )
    # 用 cast 让 mypy 接受 Literal["session", "config"]：scope 已校验。
    scope_lit: object = scope  # noqa: F841 — kept for symmetry; actual value is scope
    return Grant(
        key=key,
        scope=scope,  # type: ignore[arg-type]
        source=source,
        created_at=time.time(),
        request_id=request_id,
        session_id=session_id,
    )


def session_grant_from_config_template(grant: Grant, *, session_id: str) -> Grant:
    """把一条 ``scope=config`` 的 grant 复制为 ``scope=session``。

    helper：用户在交互中选 [s] 而非 [p] 时，可以把 [p] 的 grant 模板降级
    为本会话 grant，避免重复构造 GrantKey。

    ``session_id`` 必传：session grant 必须绑定到具体 session_id（修 P1 bug）。
    """
    return replace(
        grant,
        scope="session",
        source=DecisionSource.SESSION,
        session_id=session_id,
    )


def normalized_path_matcher(path: str) -> str:
    """规范化 path matcher：去掉冗余 ``./``，POSIX 化分隔符。

    简单实现：``PurePosixPath`` 把 ``"./tests//integration"`` → ``"tests/integration"``。
    保留尾斜杠（用户可能想表达"目录"语义）。
    """
    if not path:
        return path
    has_trailing_slash = path.endswith("/")
    cleaned = str(PurePosixPath(path))
    if has_trailing_slash and not cleaned.endswith("/"):
        cleaned += "/"
    return cleaned


__all__ = [
    "GrantStore",
    "grant_with_now",
    "normalized_path_matcher",
    "session_grant_from_config_template",
]
