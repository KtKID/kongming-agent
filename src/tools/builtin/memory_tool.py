"""Memory tool：让 Agent 查看和维护长期记忆。

v0.1.3 提供四个 action：

- ``view``：查看指定分区的活态条目。
- ``add``：追加新条目到指定分区。
- ``replace``：精确替换指定分区中的片段。
- ``remove``：精确删除指定分区中的片段。

写入 action 经过内容扫描和去重，走 safety_write 内核。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.contracts import EventSink, ToolContext
from memory import (
    ENTRY_DELIMITER,
    MEMORY_MAX_CHARS,
    USER_MAX_CHARS,
    MemoryEntry,
    MemoryStore,
    MemoryTarget,
    MemoryWriteAction,
    execute_write,
)
from tools.runtime.base import BaseBuiltinTool

# view 返回的最大字符数
_VIEW_MAX_CHARS = 8000

_TARGET_LABELS: dict[MemoryTarget, str] = {
    "memory": "MEMORY (your personal notes)",
    "user": "USER PROFILE (who the user is)",
    "errors": "ERRORS (error patterns and fixes)",
}

_TARGET_MAX_CHARS: dict[MemoryTarget, int] = {
    "memory": MEMORY_MAX_CHARS,
    "user": USER_MAX_CHARS,
    "errors": MEMORY_MAX_CHARS,  # errors 共用 memory 上限
}


class MemoryTool(BaseBuiltinTool):
    """长期记忆维护工具。

    Args:
        store: 活态 ``MemoryStore`` 实例。
        view_max_chars: view 返回的最大字符数。
        event_sinks: 事件 sink 列表，透传给 :func:`memory.execute_write` 以便写入
            操作 emit ``memory.write.*`` 事件。默认空元组（不 emit）。
    """

    name = "memory"
    description = (
        "跨会话持久化的长期记忆系统。"
        "当你需要记住用户偏好、环境事实、错误坑点或任何下次对话需要继承的信息时，"
        "必须使用本工具，不要用 write_file/shell 工具手动创建文件。"
        "记忆文件存储于 .kongming/memory/ 目录，但你只能通过本工具的 target 参数"
        "(memory/user/errors) 访问，不能通过文件路径访问。"
        "操作类型:view 查看 / add 追加 / replace 替换片段 / remove 删除片段。"
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["view", "add", "replace", "remove"],
                "description": "要执行的操作。",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user", "errors"],
                "description": (
                    "要操作的记忆分区：memory=Agent 工作笔记 / user=用户画像 / errors=错误坑点。"
                ),
            },
            "content": {
                "type": "string",
                "description": "add 操作追加的内容。",
            },
            "old_text": {
                "type": "string",
                "description": "replace 操作要查找的原文。",
            },
            "new_text": {
                "type": "string",
                "description": "replace 操作的替换文本。",
            },
            "text": {
                "type": "string",
                "description": "remove 操作要删除的文本。",
            },
            "reason": {
                "type": "string",
                "description": "本次变更的理由（可选）。",
            },
        },
        "required": ["action", "target"],
    }

    def __init__(
        self,
        store: MemoryStore,
        *,
        view_max_chars: int = _VIEW_MAX_CHARS,
        event_sinks: Sequence[EventSink] = (),
    ) -> None:
        super().__init__()
        self._store = store
        self._view_max_chars = view_max_chars
        self._event_sinks: tuple[EventSink, ...] = tuple(event_sinks)

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        action: str = args["action"]
        target: MemoryTarget = args["target"]

        if action == "view":
            return self._do_view(target)
        elif action == "add":
            return await self._do_add(target, args, ctx)
        elif action == "replace":
            return await self._do_replace(target, args, ctx)
        elif action == "remove":
            return await self._do_remove(target, args, ctx)
        else:
            return f"Unknown action: {action!r}", None

    def _do_view(self, target: MemoryTarget) -> tuple[str, dict[str, Any] | None]:
        """查看指定分区的活态条目。"""
        entries = self._get_entries(target)
        max_chars = _TARGET_MAX_CHARS.get(target, MEMORY_MAX_CHARS)

        # 计算当前文本长度
        current_text = ENTRY_DELIMITER.join(e.content for e in entries) if entries else ""
        current_len = len(current_text)
        usage_pct = current_len / max_chars * 100 if max_chars > 0 else 0
        usage_str = f"{min(usage_pct, 100):.0f}% \u2014 {current_len}/{max_chars} chars"

        if not entries:
            label = _TARGET_LABELS.get(target, target)
            return (
                f"{label} is empty. Use 'add' to create entries.",
                {
                    "success": True,
                    "target": target,
                    "entries": [],
                    "usage": usage_str,
                    "entry_count": 0,
                },
            )

        # 渲染条目
        entry_texts = [e.content for e in entries]
        combined = ENTRY_DELIMITER.join(entry_texts)

        # 截断保护
        truncated = False
        if len(combined) > self._view_max_chars:
            combined = combined[: self._view_max_chars] + "\n... (truncated)"
            truncated = True

        label = _TARGET_LABELS.get(target, target)
        content = f"{label} [{usage_str}]\n{combined}"

        data = {
            "success": True,
            "target": target,
            "entries": entry_texts,
            "usage": usage_str,
            "entry_count": len(entries),
        }
        if truncated:
            data["truncated"] = True

        return content, data

    async def _do_add(
        self, target: MemoryTarget, args: dict[str, Any], ctx: ToolContext
    ) -> tuple[str, dict[str, Any] | None]:
        """追加新条目。"""
        content = args.get("content")
        if not content or not content.strip():
            return "add requires 'content' parameter.", {
                "success": False,
                "error": "missing content",
            }

        reason = args.get("reason")
        action = MemoryWriteAction(action="add", target=target, content=content, reason=reason)
        result = await execute_write(
            self._store.memory_dir,
            action,
            event_sinks=self._event_sinks,
            run_id=ctx.run_id,
        )

        if result.ok:
            # 写入成功后统一从磁盘重读刷新活态条目，保证与磁盘一致。
            new_content = await self._store.read_target(target)
            self._store.refresh_entries_for(target, new_content)
            return result.message, {
                "success": True,
                "status": result.status,
                "chars": result.chars,
                "target": target,
            }
        else:
            return result.message, {"success": False, "status": result.status, "target": target}

    async def _do_replace(
        self, target: MemoryTarget, args: dict[str, Any], ctx: ToolContext
    ) -> tuple[str, dict[str, Any] | None]:
        """精确替换片段。"""
        old_text = args.get("old_text")
        new_text = args.get("new_text")
        if not old_text:
            return "replace requires 'old_text' parameter.", {
                "success": False,
                "error": "missing old_text",
            }

        reason = args.get("reason")
        action = MemoryWriteAction(
            action="replace",
            target=target,
            old_text=old_text,
            new_text=new_text or "",
            reason=reason,
        )
        result = await execute_write(
            self._store.memory_dir,
            action,
            event_sinks=self._event_sinks,
            run_id=ctx.run_id,
        )

        if result.ok:
            new_content = await self._store.read_target(target)
            self._store.refresh_entries_for(target, new_content)
            return result.message, {
                "success": True,
                "status": result.status,
                "chars": result.chars,
                "target": target,
            }
        else:
            return result.message, {"success": False, "status": result.status, "target": target}

    async def _do_remove(
        self, target: MemoryTarget, args: dict[str, Any], ctx: ToolContext
    ) -> tuple[str, dict[str, Any] | None]:
        """精确删除片段。"""
        text = args.get("text")
        if not text:
            return "remove requires 'text' parameter.", {"success": False, "error": "missing text"}

        reason = args.get("reason")
        action = MemoryWriteAction(action="remove", target=target, text=text, reason=reason)
        result = await execute_write(
            self._store.memory_dir,
            action,
            event_sinks=self._event_sinks,
            run_id=ctx.run_id,
        )

        if result.ok:
            new_content = await self._store.read_target(target)
            self._store.refresh_entries_for(target, new_content)
            return result.message, {
                "success": True,
                "status": result.status,
                "chars": result.chars,
                "target": target,
            }
        else:
            return result.message, {"success": False, "status": result.status, "target": target}

    def _get_entries(self, target: MemoryTarget) -> list[MemoryEntry]:
        """获取指定分区的活态条目。"""
        return {
            "memory": self._store.memory_entries,
            "user": self._store.user_entries,
            "errors": self._store.error_entries,
        }.get(target, [])


def build_memory_tool(
    store: MemoryStore,
    *,
    view_max_chars: int = _VIEW_MAX_CHARS,
    event_sinks: Sequence[EventSink] = (),
) -> MemoryTool:
    """构建 MemoryTool 实例。

    Args:
        store: 活态 ``MemoryStore``。
        view_max_chars: view 返回的最大字符数。
        event_sinks: 事件 sink 列表，透传给 :class:`MemoryTool`，写入操作会借它
            emit ``memory.write.*`` 事件（需 :func:`memory.execute_write` 已加
            ``event_sinks`` 形参，由 Agent 2 的改动承接）。

    Returns:
        配置好的 :class:`MemoryTool`。
    """
    return MemoryTool(store, view_max_chars=view_max_chars, event_sinks=event_sinks)


__all__ = ["MemoryTool", "build_memory_tool"]
