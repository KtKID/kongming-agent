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
from typing import Any, cast

from core.contracts import EventSink, PreparedToolCall, ToolContext
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
        "这是跨会话持久化的长期记忆系统"
        "当你需要记住用户偏好、环境事实、错误坑点或任何下次对话需要继承的信息时，"
        "memory系统必须使用本工具操作，不要用 write_file/shell 工具手动创建或修改文件。"
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

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前校验 action/target 并冻结 action 专属参数与默认值。"""
        del context
        self._validate_args(arguments)
        action = arguments["action"]
        target = arguments["target"]
        if action not in {"view", "add", "replace", "remove"}:
            raise ValueError("action must be one of view/add/replace/remove")
        if target not in {"memory", "user", "errors"}:
            raise ValueError("target must be one of memory/user/errors")
        prepared: dict[str, Any] = {"action": action, "target": target}
        reason = arguments.get("reason")
        if reason is not None:
            if not isinstance(reason, str):
                raise ValueError("reason must be a string when provided")
            prepared["reason"] = reason
        if action == "add":
            prepared["content"] = self._required_text(arguments, "content", action)
        elif action == "replace":
            prepared["old_text"] = self._required_text(arguments, "old_text", action)
            new_text = arguments.get("new_text", "")
            if new_text is None:
                new_text = ""
            if not isinstance(new_text, str):
                raise ValueError("replace new_text must be a string when provided")
            prepared["new_text"] = new_text
        elif action == "remove":
            prepared["text"] = self._required_text(arguments, "text", action)
        return PreparedToolCall(arguments=prepared)

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        action = str(args["action"])
        target = cast(MemoryTarget, args["target"])

        if action == "view":
            return self._do_view(target)
        elif action == "add":
            return await self._do_add(target, args, ctx)
        elif action == "replace":
            return await self._do_replace(target, args, ctx)
        elif action == "remove":
            return await self._do_remove(target, args, ctx)
        raise AssertionError(f"unreachable prepared memory action: {action!r}")

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
        content = args["content"]
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
        old_text = args["old_text"]
        new_text = args["new_text"]
        reason = args.get("reason")
        action = MemoryWriteAction(
            action="replace",
            target=target,
            old_text=old_text,
            new_text=new_text,
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
        text = args["text"]
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

    @staticmethod
    def _required_text(arguments: dict[str, Any], key: str, action: str) -> str:
        """读取 action 必填文本，输入参数/字段/action，输出非空字符串。"""
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{action} requires non-empty {key!r}")
        return value


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
