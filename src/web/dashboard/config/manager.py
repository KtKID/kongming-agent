"""manage-config-tab dev-checklist #5 — :class:`ConfigManager` 单一对外入口。

本模块是 :mod:`web.dashboard.config` 子模块的**唯一对外门面**。
sibling 模块（如未来的 ``router.py``）只允许通过 :class:`ConfigManager` 的
**5 个公开方法**触达 schema / writer / restart 的能力：

- :meth:`ConfigManager.read_schema` — 暴露字段元数据 + group 显示顺序
- :meth:`ConfigManager.read_effective` — 加载当前 ``setting.yaml`` + env 覆盖
  后的扁平化运行时值 + 每字段来源（``yaml`` / ``env`` / ``default``）
- :meth:`ConfigManager.read_raw` — 读 yaml 原文 + path + mtime（用于编辑器
  视图 & 冲突检测）
- :meth:`ConfigManager.save_patch` — 差量写回 + pydantic 校验 + 计算
  restart_required_fields
- :meth:`ConfigManager.trigger_restart` — spawn ``./start.sh web restart``

**严禁**：sibling 跨过本类直接 ``import schema / writer / restart`` 或
``config_loader.load_config``。所有触达统一收口到本类，便于：

- 后续加缓存 / 锁 / 审计层时只动一处；
- 单测 mock 时只 patch 一个 facade；
- 装配点（router 注册）只持有一个 ``ConfigManager`` 实例即可。

**错误处理风格**：保留 writer 的 :class:`ConflictError` /
:class:`ValidationFailedError` 原始语义，由 :meth:`save_patch` 透传抛出，
由更上层 router 翻译成 HTTP 409 / 422。:meth:`trigger_restart` 同理透传
:class:`RestartScriptNotFoundError`。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

# 私有访问 _ENV_FIELD_PATHS：故意 import 私有常量作为**单一真源**，避免
# 再次声明一份白名单与 loader.py 漂移。一旦 loader.py 改了字段集合，
# manager.py 自动同步——而维护两份独立列表才是真正的 bug 源。
# 同时拷一份 scheduler 通过 _apply_scheduler_env_overrides 注入的字段名，
# 它们在 loader 里是硬编码而非 _ENV_FIELD_PATHS 成员，必须显式补齐。
from config_loader.loader import _ENV_FIELD_PATHS as _LOADER_ENV_FIELD_PATHS
from config_loader.loader import load_config
from web.dashboard.config import restart, schema, writer
from web.dashboard.config.restart import RestartScriptNotFoundError
from web.dashboard.config.schema import FieldMeta, FieldSource
from web.dashboard.config.writer import (
    ConflictError,
    PatchItem,
    ValidationFailedError,
    WriteResult,
)

# 重新导出 writer / restart 的异常类型（含 PatchItem DTO），让 sibling
# （router 等）只 import 本模块即可拿到「异常 + DTO + ConfigManager」全套
# ——单一对外门面的完整闭环。具体 re-export 名单见文件底 __all__。

# scheduler 字段：loader._apply_scheduler_env_overrides 硬编码了一组额外的
# KONGMING_SCHEDULER_* env，不进 _ENV_FIELD_PATHS。这里显式补齐，让
# read_effective 的 sources 判定能覆盖到。维护时与
# config_loader.loader._apply_scheduler_env_overrides 同步。
_SCHEDULER_EXTRA_ENV_FIELDS: tuple[tuple[str, ...], ...] = (
    ("scheduler", "enabled"),
    ("scheduler", "interval"),
    ("scheduler", "max_inflight"),
    ("scheduler", "approval", "mode"),
    ("scheduler", "default_max_turns"),
)

_ENV_FIELD_PATHS: tuple[tuple[str, ...], ...] = tuple(
    list(_LOADER_ENV_FIELD_PATHS) + list(_SCHEDULER_EXTRA_ENV_FIELDS)
)

_ENV_PREFIX = "KONGMING_"

# 与 schema.py 内 _DICT_LEAF_PATHS 保持一致：这两个字段在扁平化时整段算 1
# 个 leaf，不递归展开（与 UI 视角一致——dict-leaf 字段只读 / 整段编辑）。
_DICT_LEAF_PATHS: frozenset[str] = frozenset(
    {
        "model.provider_routing",
        "model.reasoning_profiles",
    }
)


# ---------------------------------------------------------------------------
# 响应 DTO（dataclass，不引入 pydantic 二次依赖）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaResponse:
    """``read_schema`` 响应：字段元数据 + group 显示顺序。"""

    fields: list[FieldMeta]
    groups: list[dict[str, str]]


@dataclass(frozen=True)
class EffectiveResponse:
    """``read_effective`` 响应：每字段当前生效值 + 来源 + 命中的 env 列表。

    ``values``：扁平 dot-path → 当前生效值（已应用 env 覆盖）。
    ``sources``：每字段来源标签（``yaml`` / ``env`` / ``default``）。
    ``env_overrides``：实际命中的 env 路径列表（dot-path 形式）。
    """

    values: dict[str, Any]
    sources: dict[str, FieldSource]
    env_overrides: list[str]


@dataclass(frozen=True)
class RawResponse:
    """``read_raw`` 响应：yaml 文件原文 + 路径 + mtime。"""

    path: str
    mtime: float
    content: str


@dataclass(frozen=True)
class SavePatchResponse:
    """``save_patch`` 响应：成功时返回新 mtime + 需重启字段清单。

    错误场景（mtime 冲突 / pydantic 校验失败）由本方法直接 ``raise``
    :class:`ConflictError` / :class:`ValidationFailedError`，**不**包成
    ``ok=False`` 返回——保留 writer 原始语义，由 router 一处翻译 HTTP status。
    """

    ok: bool
    new_mtime: float
    restart_required_fields: list[str]


@dataclass(frozen=True)
class RestartResponse:
    """``trigger_restart`` 响应：spawn 成功的子进程 pid（仅供调试日志）。"""

    restarting: bool
    pid: int


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------


class ConfigManager:
    """配置管理子模块的唯一对外入口。

    sibling（如 router 等）只能调本类的 5 个方法：

    - :meth:`read_schema`
    - :meth:`read_effective`
    - :meth:`read_raw`
    - :meth:`save_patch`
    - :meth:`trigger_restart`

    禁止 sibling 直接 import :mod:`web.dashboard.config.schema` /
    :mod:`web.dashboard.config.writer` / :mod:`web.dashboard.config.restart`。
    新加能力 → 加方法到本类；不要在 sibling 拼裸函数。
    """

    def __init__(self, yaml_path: Path, repo_root: Path) -> None:
        """构造 ConfigManager。

        Args:
            yaml_path: ``setting.yaml`` 绝对路径。
            repo_root: 项目根（用于定位 ``./start.sh``）。
        """
        self._yaml_path = yaml_path
        self._repo_root = repo_root

    # ---- 1) read_schema ----------------------------------------------------

    def read_schema(self) -> SchemaResponse:
        """返回字段元数据 + group 显示顺序。

        薄壳：直接 fanout 到 :mod:`schema` 的两个查询函数。集中在本类是为了
        防止 sibling 直接 import schema——架构强约束。
        """
        return SchemaResponse(
            fields=schema.list_field_metas(),
            groups=schema.list_groups(),
        )

    # ---- 2) read_effective ------------------------------------------------

    def read_effective(self) -> EffectiveResponse:
        """加载当前 yaml + env 覆盖后的 Config，扁平化成 dot-path → value。

        ``sources`` 判定优先级：

        1. **env**：命中 env 覆盖白名单（loader._ENV_FIELD_PATHS +
           scheduler 额外 env）且环境变量已设置；
        2. **yaml**：env 未命中，但 yaml 原文里显式写了该 path；
        3. **default**：env / yaml 都没写，走 pydantic 模型默认值。

        ``env_overrides``：实际命中的 env path（dot-path 形式）。
        """
        cfg = load_config(self._yaml_path)
        dumped = cfg.model_dump(mode="json")
        values: dict[str, Any] = {}
        for path, value in _flatten_dict(dumped):
            values[path] = value

        # yaml 显式写的 leaf 集合：用 ruamel rt 解析原文，扁平化得到 path
        # 集合。yaml 没写但 pydantic 给了默认值的字段 → 视为 default。
        yaml_paths = _yaml_explicit_paths(self._yaml_path)

        # env override 命中：检查 _ENV_FIELD_PATHS 白名单 + scheduler 额外
        # env 是否在 os.environ 内。命中即覆盖 source = "env"。
        import os

        env_overrides: list[str] = []
        env_path_set: set[str] = set()
        for parts in _ENV_FIELD_PATHS:
            env_name = _ENV_PREFIX + "_".join(p.upper() for p in parts)
            if env_name in os.environ:
                dot_path = ".".join(parts)
                env_overrides.append(dot_path)
                env_path_set.add(dot_path)

        sources: dict[str, FieldSource] = {}
        for path in values:
            if path in env_path_set:
                sources[path] = "env"
            elif path in yaml_paths:
                sources[path] = "yaml"
            else:
                sources[path] = "default"

        return EffectiveResponse(
            values=values,
            sources=sources,
            env_overrides=env_overrides,
        )

    # ---- 3) read_raw -------------------------------------------------------

    def read_raw(self) -> RawResponse:
        """读 yaml 文件原文 + path + mtime。

        不缓存：每次都重读，保证 mtime 一致性（前端拿到的 mtime 必须是文件
        系统当前值，否则 save_patch 的冲突检测失效）。
        """
        content = self._yaml_path.read_text(encoding="utf-8")
        mtime = self._yaml_path.stat().st_mtime
        return RawResponse(
            path=str(self._yaml_path),
            mtime=mtime,
            content=content,
        )

    # ---- 4) save_patch -----------------------------------------------------

    def save_patch(self, patch: list[PatchItem], expected_mtime: float) -> SavePatchResponse:
        """差量写回 + 校验 + 计算 restart_required_fields。

        成功路径：

        1. 调 :func:`writer.round_trip_update` 完成"抢锁 → mtime 检查 →
           ruamel rt 写回 → pydantic 校验 → 原子 rename"全链路；
        2. 遍历 patch，按 :func:`schema.get_field_meta` 查每个 path 的
           ``restart_required``，True 的 path 汇总进
           ``restart_required_fields``；
        3. 返回 :class:`SavePatchResponse`（ok=True）。

        错误路径：直接透传 writer 抛出的 :class:`ConflictError` /
        :class:`ValidationFailedError`，由 router 翻译 HTTP 409 / 422。
        本方法**不**把错误包成 ``ok=False`` 返回——保留原始语义，单一翻译
        出口。

        Raises:
            ConflictError: mtime 冲突（HTTP 409）。
            ValidationFailedError: pydantic 校验失败（HTTP 422）。
            FileNotFoundError: yaml_path 不存在。
        """
        result: WriteResult = writer.round_trip_update(self._yaml_path, patch, expected_mtime)
        restart_fields: list[str] = []
        for item in patch:
            meta = schema.get_field_meta(item.path)
            if meta is not None and meta.restart_required:
                restart_fields.append(item.path)
        return SavePatchResponse(
            ok=True,
            new_mtime=result.new_mtime,
            restart_required_fields=restart_fields,
        )

    # ---- 5) trigger_restart -----------------------------------------------

    def trigger_restart(self) -> RestartResponse:
        """调 :func:`restart.trigger_restart`；透传脚本缺失异常。

        Raises:
            RestartScriptNotFoundError: ``./start.sh`` 不存在或不可执行。
        """
        pid = restart.trigger_restart(self._repo_root)
        return RestartResponse(restarting=True, pid=pid)


# ---------------------------------------------------------------------------
# 内部 helpers
# ---------------------------------------------------------------------------


def _flatten_dict(data: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    """与 schema test 内 ``flatten_dict`` 同语义：嵌套 dict 转 dot-path。

    - 命中 :data:`_DICT_LEAF_PATHS` 的 path 整段算 1 个 leaf（不递归）；
    - 非空嵌套 dict 继续递归；
    - 空 dict / list / scalar / None 整段算 1 个 leaf。

    返回 ``(dot_path, value)`` 对列表，保留出现顺序便于断言。
    """
    out: list[tuple[str, Any]] = []
    for k, v in data.items():
        path = f"{prefix}.{k}" if prefix else k
        if path in _DICT_LEAF_PATHS:
            out.append((path, v))
            continue
        if isinstance(v, dict) and v:
            out.extend(_flatten_dict(v, path))
        else:
            out.append((path, v))
    return out


def _yaml_explicit_paths(yaml_path: Path) -> set[str]:
    """读 yaml 原文，返回**显式写了**的 leaf dot-path 集合。

    用 ruamel rt 解析（与 writer.py 同一套，保 key 顺序 / 注释，但这里只
    取结构）。空文件 / null 文档 → 空集合。

    与 ``_flatten_dict`` 共享同一套 dict-leaf 规则，保证 sources 判定与
    values 扁平化对齐。
    """
    yaml = YAML(typ="rt")
    with yaml_path.open("r", encoding="utf-8") as f:
        doc = yaml.load(f)
    if doc is None:
        return set()
    # ruamel CommentedMap 满足 dict 接口；强转为普通 dict 便于递归。
    if not isinstance(doc, dict):
        return set()
    flat: list[tuple[str, Any]] = _flatten_dict(dict(doc))
    return {path for path, _ in flat}


__all__ = [
    "ConfigManager",
    "ConflictError",
    "EffectiveResponse",
    "PatchItem",
    "RawResponse",
    "RestartResponse",
    "RestartScriptNotFoundError",
    "SavePatchResponse",
    "SchemaResponse",
    "ValidationFailedError",
]
