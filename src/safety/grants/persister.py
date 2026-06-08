"""safety v0.1.4 M5.5 — Grant atomic yaml 写回。

把 :class:`Grant` 持久化到项目 yaml 配置文件（默认 ``.kongming/config.yaml``）
的 ``safety.allow_writes`` / ``safety.allow_tools_silent`` 列表。

设计要点：

- **保注释 + 保 key 顺序**：用 ``ruamel.yaml.YAML(typ="rt")`` round-trip 模式。
  pyyaml 的 ``safe_dump`` 会丢失注释 + 重排 key，不可接受。
- **原子写**：先写 ``<config_path>.tmp`` 再 ``os.replace`` 重命名。即使写入
  中途断电，也只有"老文件保留"或"新文件完整"两种状态。
- **追加而非替换**：已有的 ``allow_writes`` / ``allow_tools_silent`` 条目
  保留；新条目去重后追加到末尾。
- **降级**：``ruamel.yaml`` 可能未安装（用户尚未 ``uv sync``）；本模块在
  import 时探测，失败时降级到 ``yaml.safe_dump``——能写但会丢注释。
  fallback 路径在文件头部注入一行 warning 注释提示用户。

调用约定：

- ``persist_grant(grant, config_path)``：把单条 grant 写回。
- ``persist_grants(grants, config_path)``：批量写回（一次 yaml 读 + 多条
  追加 + 一次写）。这是 :meth:`GrantStore.take_pending_persist` 的下游消费者。

不依赖：

- 不 import :class:`GrantStore`（双向依赖隐患）。
- 不 import ``cli/`` / ``tools/`` / ``host/``。
"""

from __future__ import annotations

import os
import warnings
from io import StringIO
from pathlib import Path

from safety.approval.types import BoundaryKind, Grant

# ``allow_writes`` 写入用的 capability 常量（与 grant_store.py 保持同步）
_FILE_WRITE_CAPABILITY = "file_write"
_TOOL_CAPABILITY_PREFIX = "tool:"

# yaml 安全字段名（与 SafetyConfig 模型对齐）
_SAFETY_KEY = "safety"
_ALLOW_WRITES_KEY = "allow_writes"
_ALLOW_TOOLS_SILENT_KEY = "allow_tools_silent"

# fallback 模式下注入的提示注释
_FALLBACK_WARNING_HEADER = (
    "# WARNING: ruamel.yaml not installed; safety grant persist used pyyaml fallback.\n"
    "# Original comments / formatting may have been lost. Run `uv sync` to restore.\n"
)


# ---------------------------------------------------------------------------
# ruamel.yaml 探测（import 时一次性，运行时不重复）
# ---------------------------------------------------------------------------


def _try_import_ruamel() -> tuple[object, bool]:
    """探测 ``ruamel.yaml`` 是否可用。

    返回 ``(YAML 类或 None, available)``。可用时返回 ``(YAML, True)``，
    不可用时返回 ``(None, False)`` 并触发一次 warnings.warn。
    """
    try:
        from ruamel.yaml import YAML
    except ImportError:
        warnings.warn(
            "ruamel.yaml not installed; safety grant persist will fall back to "
            "pyyaml (comments / formatting may be lost). Run `uv sync` to install.",
            RuntimeWarning,
            stacklevel=2,
        )
        return None, False
    return YAML, True


_YAML_CLS, _RUAMEL_AVAILABLE = _try_import_ruamel()


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def persist_grant(grant: Grant, config_path: Path) -> None:
    """把单条 grant 写回 yaml 配置文件（原子）。

    See :func:`persist_grants` for details. 本函数等价于 ``persist_grants([grant], config_path)``。
    """
    persist_grants((grant,), config_path)


def persist_grants(grants: tuple[Grant, ...] | list[Grant], config_path: Path) -> None:
    """把一批 grant 批量写回 yaml 配置文件。

    流程：

    1. 验证全部 ``grant.key.boundary_kind == HOST``（v0.1.4 host-only）。
    2. 读 ``config_path`` 的现有 yaml；不存在时构造空 dict。
    3. 把每条 grant 按 capability 路由到 ``safety.allow_writes`` 或
       ``safety.allow_tools_silent`` 列表，去重追加。
    4. 写 ``<config_path>.tmp`` → ``os.replace(tmp, config_path)``。

    Args:
        grants: 待写入的 grant tuple/list。空 tuple 直接 return（no-op）。
        config_path: yaml 文件绝对路径。父目录必须已存在；不自动创建。

    Raises:
        ValueError: 任意 grant 的 boundary_kind != HOST，或 capability 无法路由。
        FileNotFoundError: ``config_path`` 父目录不存在。
        OSError: 文件 IO 失败（权限 / 磁盘满 等）。
    """
    if not grants:
        return

    # 1. 校验
    for g in grants:
        if g.key.boundary_kind != BoundaryKind.HOST:
            raise ValueError(
                f"persist_grants v0.1.4 only accepts HOST boundary_kind; "
                f"got {g.key.boundary_kind!r}"
            )
        if not _is_routable_capability(g.key.capability):
            raise ValueError(
                f"persist_grants cannot route capability={g.key.capability!r}; "
                f"only 'file_write' or 'tool:<name>' are supported in v0.1.4."
            )

    parent = config_path.parent
    if not parent.exists():
        raise FileNotFoundError(
            f"persist_grants: parent directory {parent!s} does not exist; "
            "create it before persisting."
        )

    # 2 + 3 + 4：分支按 ruamel 是否可用
    if _RUAMEL_AVAILABLE:
        _persist_with_ruamel(grants, config_path)
    else:
        _persist_with_pyyaml(grants, config_path)


# ---------------------------------------------------------------------------
# ruamel.yaml 路径（保注释 + key 顺序）
# ---------------------------------------------------------------------------


def _persist_with_ruamel(grants: tuple[Grant, ...] | list[Grant], config_path: Path) -> None:
    """ruamel.yaml round-trip 写回。"""
    assert _YAML_CLS is not None  # 由 _RUAMEL_AVAILABLE 保证
    yaml_cls = _YAML_CLS
    yaml = yaml_cls(typ="rt")  # type: ignore[operator]
    yaml.preserve_quotes = True
    # ruamel 默认 indent=2/2，与项目 setting.yaml 风格一致；不强制覆盖。

    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            doc = yaml.load(f)
        if doc is None:
            doc = {}
    else:
        doc = {}

    # 处理 safety 节
    safety_section = doc.get(_SAFETY_KEY)
    if safety_section is None:
        safety_section = {}
        doc[_SAFETY_KEY] = safety_section

    _merge_grants_into_section(grants, safety_section)

    # atomic write：tmp → replace
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.dump(doc, f)
    os.replace(tmp_path, config_path)


# ---------------------------------------------------------------------------
# pyyaml fallback（丢注释，但功能可用）
# ---------------------------------------------------------------------------


_FALLBACK_HEADER_FIRST_LINE = "# WARNING: ruamel.yaml not installed"


def _persist_with_pyyaml(grants: tuple[Grant, ...] | list[Grant], config_path: Path) -> None:
    """pyyaml fallback：注释 / 顺序丢失，但保证功能可用。

    幂等 WARNING：多次写入不重复累积 header；写前剔除已存在的 WARNING block 再注入一次。
    """
    import yaml as pyyaml  # 局部 import：避免污染顶层

    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            raw_text = f.read()
        # 剔除已有 WARNING block（任何以 _FALLBACK_HEADER_FIRST_LINE 起的连续 # 行）
        lines = raw_text.splitlines(keepends=True)
        cleaned: list[str] = []
        skip = False
        for line in lines:
            if not skip and line.startswith(_FALLBACK_HEADER_FIRST_LINE):
                skip = True
                continue
            if skip:
                if line.startswith("#"):
                    continue
                skip = False
            cleaned.append(line)
        cleaned_text = "".join(cleaned)
        doc = pyyaml.safe_load(cleaned_text) or {}
    else:
        doc = {}

    safety_section = doc.setdefault(_SAFETY_KEY, {})
    _merge_grants_into_section(grants, safety_section)

    buffer = StringIO()
    buffer.write(_FALLBACK_WARNING_HEADER)
    pyyaml.safe_dump(doc, buffer, sort_keys=False, allow_unicode=True)

    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        f.write(buffer.getvalue())
    os.replace(tmp_path, config_path)


# ---------------------------------------------------------------------------
# 共享：把 grants 合并到 safety section
# ---------------------------------------------------------------------------


def _merge_grants_into_section(
    grants: tuple[Grant, ...] | list[Grant],
    safety_section: object,
) -> None:
    """把 grants 路由到 ``safety_section`` 的两个 list 字段，去重追加。

    ``safety_section`` 在 ruamel 路径是 ``CommentedMap``，在 pyyaml 路径是
    普通 dict——两者都支持 ``__contains__`` / ``__getitem__`` / ``__setitem__``，
    所以这里用 duck typing 不区分类型。
    """
    # 类型 narrow：runtime 都是 dict-like；list 是 list-like 序列。
    # ruamel 下的 CommentedMap / CommentedSeq 也满足同样的 dunder 接口，
    # 这里用 typing.Any 避免 mypy 对 ruamel 类型一无所知（依赖未声明 stubs）。
    from typing import Any as _Any

    section: _Any = safety_section

    allow_writes_list: _Any = section.get(_ALLOW_WRITES_KEY)
    if allow_writes_list is None:
        allow_writes_list = []
        section[_ALLOW_WRITES_KEY] = allow_writes_list

    allow_tools_list: _Any = section.get(_ALLOW_TOOLS_SILENT_KEY)
    if allow_tools_list is None:
        allow_tools_list = []
        section[_ALLOW_TOOLS_SILENT_KEY] = allow_tools_list

    for grant in grants:
        capability = grant.key.capability
        matcher = grant.key.matcher
        if capability == _FILE_WRITE_CAPABILITY:
            if not _list_contains(allow_writes_list, matcher):
                allow_writes_list.append(matcher)
        elif capability.startswith(_TOOL_CAPABILITY_PREFIX):
            tool_name = capability[len(_TOOL_CAPABILITY_PREFIX) :]
            if not _list_contains(allow_tools_list, tool_name):
                allow_tools_list.append(tool_name)
        # else: 已被 _is_routable_capability 阻止，不会到这


def _list_contains(seq: object, item: str) -> bool:
    """ruamel CommentedSeq + 普通 list 都支持 ``in``，但显式写出便于阅读。"""
    try:
        return item in seq  # type: ignore[operator]
    except TypeError:
        return False


def _is_routable_capability(capability: str) -> bool:
    """capability 是否能被路由到某个 yaml 字段。"""
    if capability == _FILE_WRITE_CAPABILITY:
        return True
    return capability.startswith(_TOOL_CAPABILITY_PREFIX)


# ---------------------------------------------------------------------------
# 调试 helper（仅供测试 / 故障定位）
# ---------------------------------------------------------------------------


def is_ruamel_available() -> bool:
    """是否走 ruamel.yaml 路径（True）还是 pyyaml fallback（False）。"""
    return _RUAMEL_AVAILABLE


__all__ = ["is_ruamel_available", "persist_grant", "persist_grants"]
