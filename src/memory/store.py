"""本地长期记忆文件读写与冻结快照。

v0.1.3 Memory Snapshot：多文件读取（MEMORY / USER / ERRORS）、
冻结态 snapshot + 活态 entries 双分区、prompt 渲染。

核心语义（对齐 Hermes 冻结快照模型）：

- ``load_from_disk()`` 是唯一冻结态刷新入口。
- ``format_for_system_prompt()`` 只读冻结态，保证普通 turn 的 prompt 稳定。
- memory tool 只读写活态 entries 和磁盘，保证工具结果准确。
- history compact 后重新 ``load_from_disk()``，刷新冻结态。

外部访问入口：``src/tools/builtin/memory_tool.py::MemoryTool``。Agent 只能通过 MemoryTool
的 target 参数（memory/user/errors）访问记忆，不能直接操作文件路径——这是为了
防止 Agent 绕过 memory 管理用 write_file 建 MEMORY.md 这类"野文件"。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DEFAULT_MEMORY_DIR = Path(".kongming/memory")
_MEMORY_FILENAME = "MEMORY.md"
_USER_FILENAME = "USER.md"
_ERRORS_FILENAME = "ERRORS.md"
_DEFAULT_TEMPLATE = """\
# Agent Memory

## 用户偏好

## 环境事实

## 工具与路径

## 错误与修复记录
"""

ENTRY_DELIMITER = "\n§\n"
"""条目分隔符，与 Hermes 一致。"""

MEMORY_MAX_CHARS = 2200
"""MEMORY 分区建议上限字符数（用于 usage 显示）。"""

USER_MAX_CHARS = 1375
"""USER 分区建议上限字符数（用于 usage 显示）。"""

# 保留下划线别名用于模块内部引用。外部调用方应使用不带下划线的公开常量。
_MEMORY_MAX_CHARS = MEMORY_MAX_CHARS
_USER_MAX_CHARS = USER_MAX_CHARS

# ---------------------------------------------------------------------------
# 类型别名 & 数据结构
# ---------------------------------------------------------------------------

MemoryTarget = Literal["memory", "user", "errors"]
"""记忆分区标识。"""

_TARGET_FILENAME: dict[MemoryTarget, str] = {
    "memory": _MEMORY_FILENAME,
    "user": _USER_FILENAME,
    "errors": _ERRORS_FILENAME,
}


@dataclass(frozen=True)
class MemoryEntry:
    """单条活态记忆条目。

    Attributes:
        target: 所属分区。
        content: 条目文本（原始 Markdown 段落）。
    """

    target: MemoryTarget
    content: str


@dataclass(frozen=True)
class MemorySnapshot:
    """冻结态记忆快照，用于 system prompt 注入。

    snapshot 在 ``load_from_disk()`` 时捕获，之后保持不变。
    只有 history compact 后重新 ``load_from_disk()`` 才会刷新。

    Attributes:
        memory_text: MEMORY.md 的完整文本。
        user_text: USER.md 的完整文本。
        captured_at_ms: 快照捕获时间（毫秒时间戳）。
        source_paths: 实际参与的文件路径。
        checksum: ``sha256:<hex>``，输入为 ``memory_text + "\\n" + user_text``。
    """

    memory_text: str
    user_text: str
    captured_at_ms: int
    source_paths: tuple[str, ...]
    checksum: str

    @property
    def is_empty(self) -> bool:
        """快照是否为空（两个分区都没有内容）。"""
        return not self.memory_text.strip() and not self.user_text.strip()

    def render_prompt(self) -> str | None:
        """渲染为 system prompt block。

        对齐 Hermes 的 ``══...header...══`` 包裹格式，header 包含 usage 百分比。

        Returns:
            渲染后的文本；快照为空时返回 ``None``。
        """
        if self.is_empty:
            return None

        blocks: list[str] = []

        if self.memory_text.strip():
            usage_pct = len(self.memory_text) / _MEMORY_MAX_CHARS * 100
            pct_str = f"{min(usage_pct, 100):.0f}%"
            char_str = f"{len(self.memory_text)}/{_MEMORY_MAX_CHARS} chars"
            sep = "\u2550" * 50
            header = f"MEMORY (your personal notes) [{pct_str} \u2014 {char_str}]"
            blocks.append(f"{sep}\n{header}\n{sep}\n{self.memory_text}")

        if self.user_text.strip():
            usage_pct = len(self.user_text) / _USER_MAX_CHARS * 100
            pct_str = f"{min(usage_pct, 100):.0f}%"
            char_str = f"{len(self.user_text)}/{_USER_MAX_CHARS} chars"
            sep = "\u2550" * 50
            header = f"USER PROFILE (who the user is) [{pct_str} \u2014 {char_str}]"
            blocks.append(f"{sep}\n{header}\n{sep}\n{self.user_text}")

        if not blocks:
            return None

        return "\n\n".join(blocks)


@dataclass(frozen=True)
class MemoryWriteAction:
    """统一写入动作描述。

    Attributes:
        action: 操作类型。
        target: 目标分区。
        content: ``add`` 使用的追加内容。
        old_text: ``replace`` 使用的被替换文本。
        new_text: ``replace`` 使用的替换文本。
        text: ``remove`` 使用的待删除文本。
        reason: 操作理由（可选，用于 trace）。
    """

    action: Literal["add", "replace", "remove"]
    target: MemoryTarget
    content: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    text: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class MemoryWriteResult:
    """写入操作结构化结果。

    Attributes:
        ok: 操作是否成功。
        status: 结果状态。
        message: 人类可读的状态描述。
        target: 目标分区。
        path: 目标文件路径。
        chars: 写入/修改后的文件字符数。
    """

    ok: bool
    status: Literal["written", "skipped", "rejected", "not_found", "error"]
    message: str
    target: MemoryTarget
    path: str
    chars: int = 0


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------


def _compute_checksum(memory_text: str, user_text: str) -> str:
    """计算 snapshot checksum。"""
    raw = memory_text + "\n" + user_text
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _split_entries(text: str, target: MemoryTarget) -> list[MemoryEntry]:
    """按 ``ENTRY_DELIMITER`` 拆分文本为条目列表，去重保序。"""
    if not text.strip():
        return []
    parts = text.split(ENTRY_DELIMITER)
    # 去除空白段，去重保序
    seen: dict[str, None] = {}
    unique: list[str] = []
    for p in parts:
        stripped = p.strip()
        if stripped and stripped not in seen:
            seen[stripped] = None
            unique.append(stripped)
    return [MemoryEntry(target=target, content=c) for c in unique]


class MemoryStore:
    """读写本地 memory 目录下的文件。

    v0.1.3 扩展为多文件读取 + 冻结快照 + 活态条目：

    - ``load_from_disk()`` 创建目录、读取三文件、刷新活态条目、捕获冻结快照。
    - ``format_for_system_prompt()`` 只读冻结快照。
    - ``load()`` / ``ensure()`` 保持向后兼容。

    Args:
        base_path: 项目根目录。默认为当前工作目录；实际 memory 目录会拼为
            ``base_path / .kongming/memory``。仅在 ``memory_dir`` 为 ``None`` 时生效。
        memory_dir: 直接指定 memory 目录的绝对路径。若提供，则忽略 ``base_path``
            且不再追加 ``.kongming/memory``，让调用方（例如 cli 按 config
            ``evolution.memory.root_path`` 配置绝对路径）自由选择任意目录。
        read_max_chars: 单文件读取最大字符数。超出部分按字符截断，防止异常巨大的
            memory 文件吃掉 prompt 预算。
    """

    def __init__(
        self,
        base_path: Path | None = None,
        *,
        memory_dir: Path | None = None,
        read_max_chars: int = 65536,
    ) -> None:
        if memory_dir is not None:
            self._memory_dir = Path(memory_dir).resolve()
            self._base = self._memory_dir  # 保留属性以便老代码访问，语义不再重要
        else:
            self._base = (base_path or Path.cwd()).resolve()
            self._memory_dir = self._base / _DEFAULT_MEMORY_DIR

        self._read_max_chars = int(read_max_chars)

        # 冻结态
        self._snapshot: MemorySnapshot | None = None

        # 活态条目
        self._memory_entries: list[MemoryEntry] = []
        self._user_entries: list[MemoryEntry] = []
        self._error_entries: list[MemoryEntry] = []

    # ---- 属性 -----------------------------------------------------------

    @property
    def memory_file_path(self) -> Path:
        """返回 MEMORY.md 的绝对路径（即使文件不存在）。"""
        return self._memory_dir / _MEMORY_FILENAME

    @property
    def snapshot(self) -> MemorySnapshot | None:
        """当前冻结快照。"""
        return self._snapshot

    @property
    def memory_entries(self) -> list[MemoryEntry]:
        """活态 MEMORY 条目（可能包含写入后但未重新 snapshot 的新内容）。"""
        return list(self._memory_entries)

    @property
    def user_entries(self) -> list[MemoryEntry]:
        """活态 USER 条目。"""
        return list(self._user_entries)

    @property
    def error_entries(self) -> list[MemoryEntry]:
        """活态 ERRORS 条目。"""
        return list(self._error_entries)

    # ---- 核心方法 -------------------------------------------------------

    async def load_from_disk(self) -> MemorySnapshot:
        """从磁盘读取所有 memory 文件，刷新活态条目和冻结快照。

        这是唯一冻结态刷新入口。history compact 后应调用此方法重新加载。

        行为：
            1. 创建目录（``mkdir(exist_ok=True)``），不创建文件。
            2. 读取 MEMORY.md、USER.md、ERRORS.md（文件不存在视为空）。
            3. 按 ``ENTRY_DELIMITER`` 拆分条目，去重保序。
            4. 刷新活态 entries。
            5. 捕获新的冻结 ``MemorySnapshot``。

        Returns:
            新捕获的冻结快照。
        """
        await asyncio.to_thread(lambda: self._memory_dir.mkdir(parents=True, exist_ok=True))

        memory_text = await self._read_file(_MEMORY_FILENAME)
        user_text = await self._read_file(_USER_FILENAME)
        errors_text = await self._read_file(_ERRORS_FILENAME)

        # 活态条目
        self._memory_entries = _split_entries(memory_text, "memory")
        self._user_entries = _split_entries(user_text, "user")
        self._error_entries = _split_entries(errors_text, "errors")

        # 冻结快照
        source_paths: list[str] = []
        for fname in (_MEMORY_FILENAME, _USER_FILENAME):
            p = self._memory_dir / fname
            if p.exists():
                source_paths.append(str(p))

        self._snapshot = MemorySnapshot(
            memory_text=memory_text,
            user_text=user_text,
            captured_at_ms=int(time.time() * 1000),
            source_paths=tuple(source_paths),
            checksum=_compute_checksum(memory_text, user_text),
        )

        return self._snapshot

    def format_for_system_prompt(self, target: MemoryTarget = "memory") -> str | None:
        """从冻结快照读取指定分区文本，用于 system prompt 注入。

        Args:
            target: ``"memory"`` 或 ``"user"``。
                ``"errors"`` 不注入 system prompt。

        Returns:
            分区文本；快照不存在或分区为空时返回 ``None``。
        """
        if self._snapshot is None:
            return None
        if target == "memory":
            text = self._snapshot.memory_text
        elif target == "user":
            text = self._snapshot.user_text
        else:
            return None
        return text if text.strip() else None

    # ---- 兼容方法 -------------------------------------------------------

    async def load(self) -> str | None:
        """读取 MEMORY.md 内容（兼容 v0.1.2 接口）。

        内部委托 ``load_from_disk()`` 后返回 MEMORY.md 文本。
        如果 snapshot 已存在且非空，直接返回快照中的 memory_text，
        不重新读取磁盘。

        Returns:
            文件的 UTF-8 文本内容；文件不存在或为空时返回 ``None``。
        """
        if self._snapshot is not None:
            text = self._snapshot.memory_text
            return text if text.strip() else None

        await self.load_from_disk()
        assert self._snapshot is not None
        text = self._snapshot.memory_text
        return text if text.strip() else None

    async def ensure(self) -> Path:
        """确保 MEMORY.md 存在（兼容 v0.1.2 接口）。

        文件已存在则跳过，不存在则创建目录并写入默认模板。

        Returns:
            MEMORY.md 的绝对路径。
        """
        target = self._memory_dir / _MEMORY_FILENAME
        if target.exists():
            return target

        def _create() -> Path:
            self._memory_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(_DEFAULT_TEMPLATE, encoding="utf-8")
            return target

        return await asyncio.to_thread(_create)

    # ---- 活态条目更新（供 safety_write 调用）----------------------------

    def refresh_entries_for(self, target: MemoryTarget, text: str) -> None:
        """刷新指定分区的活态条目（写入后调用，不触碰冻结快照）。

        Args:
            target: 分区。
            text: 该分区的新完整文本。
        """
        entries = _split_entries(text, target)
        if target == "memory":
            self._memory_entries = entries
        elif target == "user":
            self._user_entries = entries
        elif target == "errors":
            self._error_entries = entries

    # ---- 内部辅助 -------------------------------------------------------

    async def _read_file(self, filename: str) -> str:
        """读取 memory 目录下指定文件。

        文件不存在或读取失败时返回空字符串（而非 None），
        因为快照需要空字符串来表示"没有内容"。

        Args:
            filename: 文件名（不含目录）。

        Returns:
            文件内容；不存在或为空时返回空字符串。
        """
        path = self._memory_dir / filename
        read_max = self._read_max_chars

        def _sync_read() -> str:
            try:
                if not path.exists():
                    return ""
                content = path.read_text(encoding="utf-8")
                if read_max > 0 and len(content) > read_max:
                    content = content[:read_max]
                return content
            except (OSError, UnicodeDecodeError):
                return ""

        return await asyncio.to_thread(_sync_read)

    @property
    def memory_dir(self) -> Path:
        """memory 目录的绝对路径。"""
        return self._memory_dir

    def target_path(self, target: MemoryTarget) -> Path:
        """返回指定分区文件的绝对路径。"""
        return self._memory_dir / _TARGET_FILENAME[target]

    async def read_target(self, target: MemoryTarget) -> str:
        """异步读取指定分区文件的最新磁盘内容。

        文件不存在或读取失败时返回空字符串，与 :meth:`_read_file` 语义一致。
        公开接口，供 memory tool / safety_write 等消费方使用，避免访问私有方法。
        """
        return await self._read_file(_TARGET_FILENAME[target])
