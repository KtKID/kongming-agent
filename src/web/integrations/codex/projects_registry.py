"""Codex 项目登记表持久化层（v0.1）。

落盘位置：``<home>/web/codex_projects.json``，``home`` 由调用方注入
（通常来自 :func:`config_loader.get_kongming_home`）。

设计目标：

- 维护用户在 Web UI 上"加过的 codex 项目工作目录"列表，与
  ``thread_metadata.py`` 解耦——一个 cwd 可以登记后没有任何 thread，
  也可以有多条 thread 共用一个 cwd。
- 启动时 :func:`bootstrap_register_self` 把仓库自身根目录登记为默认项，
  避免空状态下用户找不到入口。
- 历史数据迁移走 :func:`migrate_from_thread_metadata`，扫已有 thread
  的 ``backend_kind == "codex"`` distinct cwd 一次性入库。

公共 API：

- :class:`ProjectRegistryEntry` —— 单条登记项 dataclass（frozen）
- :func:`codex_projects_path` —— 路径常量
- :func:`load_registry` —— 读盘，损坏静默降级
- :func:`add_project` —— 添加 / 幂等更新 alias
- :func:`remove_project` —— 删除（幂等）
- :func:`bootstrap_register_self` —— 启动时自登记仓库根
- :func:`migrate_from_thread_metadata` —— 从 thread metadata 迁移历史 cwd

落盘 schema：

```json
{
  "version": 1,
  "projects": [
    {"cwd": "/abs/path", "alias": "", "added_at": 1778115400.0}
  ]
}
```

约束：

- 所有路径均基于传入的 ``home`` 参数派生；本模块**不**调用
  ``Path.home()`` / 不读环境变量，确保 worktree 隔离与可测试性。
- 损坏文件（JSON 解析失败 / 不是 dict / projects 不是 list /
  schema_version 高于 ``CURRENT_VERSION``）一律返回 ``[]`` 并 warning，
  不抛异常。
- ``cwd`` 是否真的是目录由路由层校验（业务边界）；本模块只关心字符串
  以 ``/`` 开头，不做更严格的存在性 / 可读性检查。
- 设计上选 ``@dataclass(frozen=True)`` 而非 pydantic：仅做内部存盘 +
  返回值类型，不需要 pydantic 的 ``model_validate`` / JSON schema /
  正则校验等额外能力，dataclass 更轻、也跟 ``ThreadMetadata`` (pydantic)
  形成对比凸显职责差异。

与 ``src/web/integrations/claude_code/projects_registry.py`` 对称：仅两处差异——
1. 路径文件名 ``codex_projects.json``（vs ``claude_projects.json``）
2. :func:`migrate_from_thread_metadata` 过滤 ``backend_kind == "codex"``
   （vs ``"claude_code"``）
两份实现刻意保持独立、不抽公共模块，方便两侧各自演进。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from web.path_utils import is_absolute_workspace_path
from web.threads.metadata import list_thread_metadata

logger = logging.getLogger(__name__)


# 当前 registry 文件 schema 版本。
# 将来字段不兼容时 bump 并在 :func:`load_registry` 里加迁移分支。
CURRENT_VERSION = 1


@dataclass(frozen=True)
class ProjectRegistryEntry:
    """单条 codex 项目登记项（不可变）。

    Attributes:
        cwd: 工作目录绝对路径；调用方保证以 ``/`` 开头（POSIX）。
            登记后用作唯一键。
        alias: 用户自定义别名；空字符串表示未设置，UI 可回退展示
            目录名。
        added_at: 首次登记时间（unix 秒）；幂等 ``add_project``
            不刷新此值，便于排序按"加入先后"稳定。
    """

    cwd: str
    alias: str
    added_at: float


def codex_projects_path(home: Path) -> Path:
    """返回 registry 文件路径 ``<home>/web/codex_projects.json``。

    Args:
        home: ``.kongming/`` 根目录。

    Returns:
        绝对路径 :class:`Path`，不保证文件存在。
    """
    return home / "web" / "codex_projects.json"


def _entry_from_dict(raw: object) -> ProjectRegistryEntry | None:
    """把单条 JSON dict 转成 :class:`ProjectRegistryEntry`。

    校验项：

    - ``raw`` 必须是 ``dict``
    - ``cwd`` 必须是非空且以 ``/`` 开头的 str
    - ``alias`` 必须是 str（缺省补 ``""``）
    - ``added_at`` 必须是 int / float（缺省补 ``0.0``）

    任一不满足返回 ``None`` 并 warning。
    """
    if not isinstance(raw, dict):
        logger.warning("project registry entry not a dict: %r", raw)
        return None
    cwd = raw.get("cwd")
    if not isinstance(cwd, str) or not is_absolute_workspace_path(cwd):
        logger.warning("project registry entry has invalid cwd: %r", raw)
        return None
    alias_raw = raw.get("alias", "")
    alias = alias_raw if isinstance(alias_raw, str) else ""
    added_at_raw = raw.get("added_at", 0.0)
    added_at = float(added_at_raw) if isinstance(added_at_raw, (int, float)) else 0.0
    return ProjectRegistryEntry(cwd=cwd, alias=alias, added_at=added_at)


def load_registry(home: Path) -> list[ProjectRegistryEntry]:
    """读 registry 文件，返回登记项列表。

    返回 ``[]`` 的情况（均 warning，不抛）：

    - 文件不存在
    - 读盘 OSError
    - JSON 解析失败
    - 顶层不是 dict
    - ``projects`` 字段缺失或不是 list
    - ``schema_version`` 高于 :data:`CURRENT_VERSION`（前向兼容拒绝策略）

    单条 entry 校验失败的不会拖垮整个文件——跳过该条继续。
    """
    path = codex_projects_path(home)
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("failed to read codex projects registry at %s: %s", path, exc)
        return []
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("codex projects registry JSON corrupted at %s: %s", path, exc)
        return []
    if not isinstance(data, dict):
        logger.warning("codex projects registry not a JSON object at %s", path)
        return []
    version_raw = data.get("version", CURRENT_VERSION)
    if not isinstance(version_raw, int):
        logger.warning(
            "codex projects registry version not int at %s: %r",
            path,
            version_raw,
        )
        return []
    if version_raw > CURRENT_VERSION:
        logger.warning(
            "codex projects registry version %s exceeds known %s at %s; treating as empty",
            version_raw,
            CURRENT_VERSION,
            path,
        )
        return []
    projects_raw = data.get("projects")
    if not isinstance(projects_raw, list):
        logger.warning(
            "codex projects registry has no projects list at %s: %r",
            path,
            projects_raw,
        )
        return []
    out: list[ProjectRegistryEntry] = []
    for item in projects_raw:
        entry = _entry_from_dict(item)
        if entry is not None:
            out.append(entry)
    return out


def _write_registry(home: Path, entries: list[ProjectRegistryEntry]) -> None:
    """原子写盘 registry。

    实现：先写 ``codex_projects.json.tmp``，再 ``os.replace`` 到目标
    路径——POSIX / Windows 同文件系统下都是原子操作，避免半写文件
    导致下次 :func:`load_registry` 解析失败。

    父目录 ``<home>/web/`` 缺失时自动创建。
    """
    path = codex_projects_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CURRENT_VERSION,
        "projects": [asdict(entry) for entry in entries],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def add_project(home: Path, cwd: str, alias: str = "") -> ProjectRegistryEntry:
    """登记一个 cwd；同 cwd 已存在则幂等更新 alias，``added_at`` 保留。

    Args:
        home: ``.kongming/`` 根目录。
        cwd: 工作目录绝对路径，调用方保证以 ``/`` 开头。本模块不
            做 ``is_dir`` / 存在性校验——业务校验留给路由层。
        alias: 别名，空字符串表示无。

    Returns:
        写盘后的 :class:`ProjectRegistryEntry`。如果是更新分支，
        返回的 ``added_at`` 是首次登记时的值。

    实现：
    - 加载现有 entries，定位是否有同 cwd
    - 命中 → 用旧 ``added_at`` + 新 ``alias`` 重建 entry，替换原位
    - 未命中 → 追加新 entry，``added_at = time.time()``
    - 写盘（原子）
    """
    entries = load_registry(home)
    target: ProjectRegistryEntry
    found_index = -1
    for i, entry in enumerate(entries):
        if entry.cwd == cwd:
            found_index = i
            break
    if found_index >= 0:
        existing = entries[found_index]
        target = ProjectRegistryEntry(
            cwd=cwd,
            alias=alias,
            added_at=existing.added_at,
        )
        entries[found_index] = target
    else:
        target = ProjectRegistryEntry(
            cwd=cwd,
            alias=alias,
            added_at=time.time(),
        )
        entries.append(target)
    _write_registry(home, entries)
    return target


def remove_project(home: Path, cwd: str) -> bool:
    """删除一条 cwd 登记。

    Args:
        home: ``.kongming/`` 根目录。
        cwd: 待删 cwd。

    Returns:
        ``True`` 命中并已写盘删除；``False`` 未命中（不写盘，幂等）。
    """
    entries = load_registry(home)
    new_entries = [e for e in entries if e.cwd != cwd]
    if len(new_entries) == len(entries):
        return False
    _write_registry(home, new_entries)
    return True


def bootstrap_register_self(home: Path, repo_root: str) -> None:
    """启动时把当前仓库根目录登记进 registry。

    两种触发分支：

    1. registry 文件不存在 —— 创建空 registry 并登记 ``repo_root``
    2. 文件已存在但 ``repo_root`` 不在其中 —— 追加登记

    若 ``repo_root`` 已登记，函数无副作用（不刷新 ``alias``，不刷新
    ``added_at``）；这是为了让用户手动改过的 alias 不被启动逻辑覆盖。

    Args:
        home: ``.kongming/`` 根目录。
        repo_root: 仓库根目录绝对路径。本函数不做 ``is_dir`` 校验，
            调用方保证。
    """
    path = codex_projects_path(home)
    if not path.is_file():
        # 文件不存在 → 直接 add（add_project 内部 load_registry 返回 []）
        add_project(home, repo_root, alias="")
        return
    entries = load_registry(home)
    if any(e.cwd == repo_root for e in entries):
        return
    add_project(home, repo_root, alias="")


def migrate_from_thread_metadata(home: Path) -> int:
    """从 thread metadata 反向迁移 codex distinct cwd 到 registry。

    扫 ``<home>/web/threads/*/metadata.json``，对所有
    ``backend_kind == "codex"`` 且 ``cwd != ""`` 的记录取 distinct cwd，
    依次 :func:`add_project`。

    Returns:
        新增的登记数（已存在的不计）。``add_project`` 对已存在
        cwd 是幂等更新（不改 ``added_at``），但本函数刻意只统计
        "迁移前不在 registry 中的 cwd"，方便调用方打日志告知用户。

    损坏的 metadata 由 :func:`list_thread_metadata` 内部跳过，本函数
    不需要额外容错。
    """
    existing = {e.cwd for e in load_registry(home)}
    metadatas = list_thread_metadata(home)
    seen: set[str] = set()
    added = 0
    for meta in metadatas:
        if meta.backend_kind != "codex":
            continue
        if not meta.cwd:
            continue
        if meta.cwd in seen:
            continue
        seen.add(meta.cwd)
        if meta.cwd in existing:
            # 已在 registry，跳过 add_project 调用（避免无谓写盘）
            continue
        add_project(home, meta.cwd, alias="")
        added += 1
    return added


__all__ = [
    "CURRENT_VERSION",
    "ProjectRegistryEntry",
    "add_project",
    "bootstrap_register_self",
    "codex_projects_path",
    "load_registry",
    "migrate_from_thread_metadata",
    "remove_project",
]
