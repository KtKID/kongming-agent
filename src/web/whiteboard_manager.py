"""跨 scope 白板管理器。

`WhiteboardManager` 统一管理两个独立的 whiteboard workspace：

- **global workspace**：``<whiteboard_root>/global/``，跨项目共享，所有 cwd 都能看到。
- **project workspace**：``<whiteboard_root>/projects/<encode_project_dir(cwd)>/``，
  每个 cwd 一份独立白板。目录名格式为 ``<basename-slug>-<sha256[:8]>``——
  basename 保留可读性，hash 后缀保证唯一（同 basename 不同路径不会撞）。

设计要点：

- ``scope`` 由"卡片所在 workspace 目录"决定，**不写入** ``board.json``。运行时通过
  ``_find_card_workspace`` 反推。这样底层 ``WhiteboardCardMeta`` / ``WhiteboardMeta``
  保持零改动，store 函数签名零改动。
- Manager 不重新实现 atomic write / 锁；所有持久化与并发控制都委托给 store 层。
- ``WhiteboardScopeError``：``scope="project"`` 但 cwd 为空时抛出，router 转 422。
- ``WhiteboardContentConflictError`` 由 store 抛出，Manager 原样透传，不吞掉。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from web.whiteboard_store import (
    DEFAULT_CARD_HEIGHT,
    DEFAULT_CARD_TITLE,
    DEFAULT_CARD_X,
    DEFAULT_CARD_Y,
    WhiteboardCardRecord,
    WhiteboardContentConflictError,
    WhiteboardInvalidLayoutError,
    WhiteboardLayoutUpdate,
    create_card,
    delete_card,
    load_whiteboard,
    update_card_content,
    update_whiteboard_layout,
)

CardScope = Literal["project", "global"]

_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_PROJECT_DIR_HASH_LEN = 8


def encode_project_dir(cwd: str) -> str:
    """生成 project 子目录名：可读 + 唯一 + 和路径有关。

    格式 ``<basename-slug>-<sha256[:8]>``：

    - **basename-slug**：取 ``Path(cwd).name`` 小写后保留 ``[a-z0-9-]``，其他字符
      压成 ``-``；为空 / 全非 ASCII 时退化为 ``project``。
    - **hash**：SHA-256 完整 ``cwd`` 字符串前 8 字符——hash 空间 2^32，本地工具够用，
      且保证同 basename 不同路径（如 ``/a/proj`` vs ``/b/proj``）不撞。

    示例：

    - ``/Users/kid/proj/kongming-agent`` → ``kongming-agent-<8hex>``
    - ``/Users/kid/项目/喵🐱`` → ``project-<8hex>``（非 ASCII basename 退化）
    """
    basename = Path(cwd).name.lower()
    slug = _SLUG_RE.sub("-", basename).strip("-") or "project"
    digest = hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:_PROJECT_DIR_HASH_LEN]
    return f"{slug}-{digest}"


class ScopedCardRecord(BaseModel):
    """带 scope 标记的卡片快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record: WhiteboardCardRecord
    scope: CardScope


class ScopedWhiteboardSnapshot(BaseModel):
    """跨 scope 合并后的白板快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    global_title: str
    project_title: str | None
    cards: list[ScopedCardRecord]


class WhiteboardScopeError(ValueError):
    """``scope="project"`` 但 cwd 为空时抛出。router 应转 422。"""


class WhiteboardManager:
    """跨 scope 白板管理器。"""

    def __init__(self, whiteboard_root: Path) -> None:
        self._root = whiteboard_root

    # ------------------------------------------------------------------
    # workspace 解析
    # ------------------------------------------------------------------

    def _global_workspace(self) -> Path:
        return self._root / "global"

    def _project_workspace(self, cwd: str) -> Path:
        return self._root / "projects" / encode_project_dir(cwd)

    def _write_project_meta(self, cwd: str) -> None:
        """首次落 project workspace 时，把原始 cwd 记到 ``meta.json``。

        - 已存在则不覆盖（保留首次写入的真值）。
        - utf-8 + ensure_ascii=False + indent=2。
        """
        workspace = self._project_workspace(cwd)
        meta_path = workspace / "meta.json"
        if meta_path.exists():
            return
        workspace.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps({"cwd": cwd}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _find_card_workspace(self, cwd: str | None, card_id: str) -> tuple[Path, CardScope]:
        """按 global → project 顺序查找卡片所属 workspace。

        Raises:
            KeyError: 两个 scope 都没找到时抛出 ``card_id``。
        """
        global_ws = self._global_workspace()
        global_snapshot = load_whiteboard(global_ws)
        if any(card.id == card_id for card in global_snapshot.cards):
            return global_ws, "global"

        if cwd:
            project_ws = self._project_workspace(cwd)
            if project_ws.exists():
                project_snapshot = load_whiteboard(project_ws)
                if any(card.id == card_id for card in project_snapshot.cards):
                    return project_ws, "project"

        raise KeyError(card_id)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def load(self, cwd: str | None) -> ScopedWhiteboardSnapshot:
        """加载跨 scope 合并白板快照。

        - ``cwd`` 为 ``None`` 或空字符串：只读 global，``project_title=None``。
        - 否则：合并 global + project；project 目录不存在则视为空，``project_title=None``。
        - 合并后按 ``z_index`` 升序排序。
        """
        global_ws = self._global_workspace()
        global_snapshot = load_whiteboard(global_ws)
        scoped: list[ScopedCardRecord] = [
            ScopedCardRecord(record=card, scope="global") for card in global_snapshot.cards
        ]

        project_title: str | None = None
        if cwd:
            project_ws = self._project_workspace(cwd)
            if project_ws.exists():
                project_snapshot = load_whiteboard(project_ws)
                project_title = project_snapshot.title
                scoped.extend(
                    ScopedCardRecord(record=card, scope="project")
                    for card in project_snapshot.cards
                )

        scoped.sort(key=lambda item: item.record.z_index)

        return ScopedWhiteboardSnapshot(
            global_title=global_snapshot.title,
            project_title=project_title,
            cards=scoped,
        )

    def create_card(
        self,
        cwd: str | None,
        *,
        scope: CardScope,
        title: str = DEFAULT_CARD_TITLE,
        category: str = "",
        content: str = "",
        x: int = DEFAULT_CARD_X,
        y: int = DEFAULT_CARD_Y,
        height: int = DEFAULT_CARD_HEIGHT,
        collapsed: bool = False,
    ) -> ScopedCardRecord:
        """在指定 scope 创建卡片。

        - ``scope="global"`` → 直接写入 global workspace（cwd 可为空）。
        - ``scope="project"`` 且 cwd 非空 → 先落 ``meta.json`` 再写入 project workspace。
        - ``scope="project"`` 且 cwd 空 → ``WhiteboardScopeError``。
        """
        if scope == "global":
            workspace = self._global_workspace()
        else:
            if not cwd:
                raise WhiteboardScopeError("project scope requires a non-empty cwd")
            self._write_project_meta(cwd)
            workspace = self._project_workspace(cwd)

        record = create_card(
            workspace,
            title=title,
            category=category,
            content=content,
            x=x,
            y=y,
            height=height,
            collapsed=collapsed,
        )
        return ScopedCardRecord(record=record, scope=scope)

    def update_card_content(
        self,
        cwd: str | None,
        card_id: str,
        *,
        content: str,
        expected_updated_at: float | None,
        title: str | None,
        category: str | None,
    ) -> ScopedCardRecord:
        """更新卡片正文。

        ``_find_card_workspace`` 自动判定卡片在哪个 scope。

        Raises:
            KeyError: 卡片在两个 scope 都找不到（router 转 404）。
            WhiteboardContentConflictError: ``expected_updated_at`` 与实际不符（store 抛，原样透传）。
        """
        workspace, scope = self._find_card_workspace(cwd, card_id)
        record = update_card_content(
            workspace,
            card_id,
            content=content,
            expected_updated_at=expected_updated_at,
            title=title,
            category=category,
        )
        return ScopedCardRecord(record=record, scope=scope)

    def update_layout(
        self,
        cwd: str | None,
        *,
        scope: CardScope,
        title: str | None,
        card_layouts: list[WhiteboardLayoutUpdate],
    ) -> ScopedWhiteboardSnapshot:
        """更新指定 scope 的白板 title + 卡片布局。

        - ``scope="global"`` → 改 global ``board.json``。
        - ``scope="project"`` 且 cwd 非空 → 改 project ``board.json``。
        - ``scope="project"`` 且 cwd 空 → ``WhiteboardScopeError``。
        - 返回值通过 ``self.load(cwd)`` 重新组装，保持与 ``load`` 一致的合并 + ``z_index`` 排序语义。
        """
        if scope == "global":
            workspace = self._global_workspace()
        else:
            if not cwd:
                raise WhiteboardScopeError("project scope requires a non-empty cwd")
            workspace = self._project_workspace(cwd)
            # 首写 project workspace 时备份原 cwd（幂等：已有则跳过）。
            # 修复 R2 #1：避免 layout 入口绕过 create_card 直接落盘 board.json 时漏 meta.json。
            self._write_project_meta(cwd)

        update_whiteboard_layout(
            workspace,
            title=title,
            card_layouts=card_layouts,
        )
        return self.load(cwd)

    def delete_card(self, cwd: str | None, card_id: str) -> None:
        """删除卡片。

        Raises:
            KeyError: 卡片在两个 scope 都找不到。
        """
        workspace, _scope = self._find_card_workspace(cwd, card_id)
        delete_card(workspace, card_id)


__all__ = [
    "CardScope",
    "ScopedCardRecord",
    "ScopedWhiteboardSnapshot",
    "WhiteboardContentConflictError",
    "WhiteboardInvalidLayoutError",
    "WhiteboardLayoutUpdate",
    "WhiteboardManager",
    "WhiteboardScopeError",
    "encode_project_dir",
]
