"""Memory 安全写入内核。

提供内容扫描、去重、原子写入能力，供 MemoryTool 和后续 LearningLoop 共用。

核心行为：

- 内容扫描拦截 prompt injection、secret exfiltration、越权系统指令、不可见 Unicode。
- ``add`` 写入前按 ``ENTRY_DELIMITER`` 拆分现有条目，entry-level exact duplicate 返回 skipped。
- ``replace`` / ``remove`` 使用精确片段匹配。
- 写入使用临时文件 + ``os.replace()``，失败保持旧文件。
- 写入前从磁盘重读最新状态（单进程原子替换，v0.1.3 不做 flock）。
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

from core.contracts import Event, EventSink
from memory.store import ENTRY_DELIMITER, MemoryTarget, MemoryWriteAction, MemoryWriteResult

# ---------------------------------------------------------------------------
# 文件映射
# ---------------------------------------------------------------------------

_TARGET_FILENAME: dict[MemoryTarget, str] = {
    "memory": "MEMORY.md",
    "user": "USER.md",
    "errors": "ERRORS.md",
}

# ---------------------------------------------------------------------------
# 内容扫描规则
# ---------------------------------------------------------------------------

# Prompt injection 模式
_PROMPT_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"disregard\s+(all\s+)?(above|previous)",
        r"you\s+are\s+now\s+",
        r"forget\s+(all\s+)?(your|previous|the)\s+",
        r"new\s+instructions?\s*:",
        r"override\s+(your\s+)?(system|previous)\s+",
    ]
]

# Secret exfiltration 模式
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r'(?:api[_-]?key|secret|password|token|credential)\s*[=:]\s*["\']?\S{8,}',
        r"\bsk-[a-zA-Z0-9]{20,}\b",  # OpenAI-style keys
        r"\bghp_[a-zA-Z0-9]{36,}\b",  # GitHub PATs
    ]
]

# 越权系统指令模式
#
# 只拦截"明确越权意图"的 system 标签/指令，避免误伤正常记忆内容
# （如 "macOS system: Darwin 24.6"、"the system is working"）。
_UNAUTHORIZED_SYSTEM_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"<\s*system\s*>",
        r"\[system\]",
        # 仅当 system: 后紧跟越权关键词时拦截，不再误伤普通 "system:" 文本。
        r"\bsystem\s*:\s*(?:ignore|disregard|override|forget|you\s+must|you\s+will)\b",
    ]
]

# 不可见 Unicode 字符
_INVISIBLE_UNICODE_RE = re.compile(
    "["
    "\u200b-\u200f"  # Zero-width space / non-joiner / joiner / LRM / RLM / LRE / RLO
    "\u2028-\u202e"  # Line/paragraph separators + directional overrides
    "\u2060-\u2069"  # Word joiner + invisible characters
    "\ufeff"  # BOM
    "\ufff9-\ufffb"  # Interlinear annotation
    "]"
)

# C0/C1 控制字符（排除 \t \n \r）
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def scan_content(text: str) -> str | None:
    """扫描内容是否安全。

    Args:
        text: 待写入的文本。

    Returns:
        拒绝原因；``None`` 表示通过。
    """
    # Prompt injection
    for pattern in _PROMPT_INJECTION_PATTERNS:
        m = pattern.search(text)
        if m:
            return f"suspected prompt injection: {m.group()!r}"

    # Secret exfiltration
    for pattern in _SECRET_PATTERNS:
        m = pattern.search(text)
        if m:
            return f"suspected secret exposure: {m.group()!r}"

    # Unauthorized system instructions
    for pattern in _UNAUTHORIZED_SYSTEM_PATTERNS:
        m = pattern.search(text)
        if m:
            return f"suspected unauthorized system instruction: {m.group()!r}"

    # Invisible Unicode
    m = _INVISIBLE_UNICODE_RE.search(text)
    if m:
        code = f"U+{ord(m.group()):04X}"
        return f"contains invisible unicode: {code}"

    # Control characters
    m = _CONTROL_CHAR_RE.search(text)
    if m:
        code = f"U+{ord(m.group()):04X}"
        return f"contains control character: {code}"

    return None


# ---------------------------------------------------------------------------
# 文件 I/O 辅助
# ---------------------------------------------------------------------------


def _read_file_sync(path: Path) -> str | None:
    """同步读取文件内容。文件不存在返回 ``None``。"""
    try:
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _atomic_write_sync(path: Path, content: str) -> None:
    """同步原子写入：先写临时文件，再 ``os.replace()`` 替换。

    Windows 注意：``NamedTemporaryFile(delete=False)`` 避免 rename 时被占用。
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except BaseException:
        # 清理临时文件
        import contextlib

        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


# ---------------------------------------------------------------------------
# 核心写入逻辑
# ---------------------------------------------------------------------------


async def _read_file(path: Path) -> str | None:
    """异步读取文件内容。"""
    return await asyncio.to_thread(_read_file_sync, path)


async def _atomic_write(path: Path, content: str) -> None:
    """异步原子写入。"""
    await asyncio.to_thread(_atomic_write_sync, path, content)


async def execute_write(
    memory_dir: Path,
    action: MemoryWriteAction,
    event_sinks: Sequence[EventSink] = (),
    run_id: str = "memory",
) -> MemoryWriteResult:
    """执行一次安全写入操作。

    Args:
        memory_dir: ``.kongming/memory/`` 目录的绝对路径。
        action: 写入动作描述。
        event_sinks: 事件落地目标列表（可选）。每次写入完成后会按
            ``result.status`` 映射到 ``memory.write.{success,rejected,error}``
            之一并 emit。sink 抛异常不会污染主调用链路。默认空元组表示
            不走 trace。
        run_id: emit 事件时附带的 ``run_id``。MemoryTool 可透传自己的
            run_id；非工具调用场景（LearningLoop / 测试）留默认 ``"memory"``
            占位即可。

    Returns:
        结构化写入结果。
    """
    filename = _TARGET_FILENAME[action.target]
    target_path = memory_dir / filename

    # 1. 内容扫描
    scan_text = action.content or action.new_text or action.text or ""
    if scan_text:
        rejection = scan_content(scan_text)
        if rejection is not None:
            result = MemoryWriteResult(
                ok=False,
                status="rejected",
                message=rejection,
                target=action.target,
                path=str(target_path),
            )
            await _emit_write_event(result, action, event_sinks, run_id)
            return result

    # 2. 从磁盘重读最新状态
    current = await _read_file(target_path)

    if action.action == "add":
        result = await _do_add(target_path, action, current)
    elif action.action == "replace":
        result = await _do_replace(target_path, action, current)
    elif action.action == "remove":
        result = await _do_remove(target_path, action, current)
    else:
        result = MemoryWriteResult(
            ok=False,
            status="error",
            message=f"unknown action: {action.action}",
            target=action.target,
            path=str(target_path),
        )

    await _emit_write_event(result, action, event_sinks, run_id)
    return result


# ---------------------------------------------------------------------------
# Event emit 辅助
# ---------------------------------------------------------------------------


_STATUS_TO_EVENT_KIND: dict[str, str] = {
    "written": "memory.write.success",
    "rejected": "memory.write.rejected",
    "error": "memory.write.error",
    "not_found": "memory.write.error",
    "skipped": "memory.write.error",
}


async def _emit_write_event(
    result: MemoryWriteResult,
    action: MemoryWriteAction,
    event_sinks: Sequence[EventSink],
    run_id: str,
) -> None:
    """按 result.status 映射 EventKind 并向所有 sink fan-out。

    sink 抛异常不会污染主调用链路——每个 sink 都在独立的 try/except 里 emit。
    """
    if not event_sinks:
        return

    kind = _STATUS_TO_EVENT_KIND.get(result.status, "memory.write.error")
    payload: dict[str, object] = {
        "target": action.target,
        "action": action.action,
        "status": result.status,
        "path": result.path,
        "chars": result.chars,
        "reason": action.reason,
    }
    event = Event(kind=kind, run_id=run_id, payload=payload)
    for sink in event_sinks:
        try:
            await sink.emit(event)
        except Exception:
            # sink 异常不污染主链路，也不打断对其他 sink 的 fan-out
            continue


async def _do_add(
    target_path: Path,
    action: MemoryWriteAction,
    current: str | None,
) -> MemoryWriteResult:
    """执行 add 操作。"""
    content = action.content or ""
    if not content.strip():
        return MemoryWriteResult(
            ok=False,
            status="error",
            message="add action requires non-empty content",
            target=action.target,
            path=str(target_path),
        )

    # 去重：按 ENTRY_DELIMITER 拆分条目，entry-level exact match
    if current is not None:
        existing_entries = {part.strip() for part in current.split(ENTRY_DELIMITER) if part.strip()}
        if content.strip() in existing_entries:
            return MemoryWriteResult(
                ok=False,
                status="skipped",
                message="exact duplicate, entry already exists",
                target=action.target,
                path=str(target_path),
                chars=len(current),
            )

    # 追加（使用 § 分隔符）。current 为 None 或全空时不加前导分隔符。
    if current is None or not current.strip():
        new_text = content
    else:
        new_text = current.rstrip("\n") + ENTRY_DELIMITER + content

    await _atomic_write(target_path, new_text)

    return MemoryWriteResult(
        ok=True,
        status="written",
        message="content added",
        target=action.target,
        path=str(target_path),
        chars=len(new_text),
    )


async def _do_replace(
    target_path: Path,
    action: MemoryWriteAction,
    current: str | None,
) -> MemoryWriteResult:
    """执行 replace 操作。"""
    old_text = action.old_text or ""
    new_text = action.new_text or ""

    if not old_text:
        return MemoryWriteResult(
            ok=False,
            status="error",
            message="replace action requires old_text",
            target=action.target,
            path=str(target_path),
        )

    if current is None or old_text not in current:
        return MemoryWriteResult(
            ok=False,
            status="not_found",
            message="old_text not found in target file",
            target=action.target,
            path=str(target_path),
        )

    replaced = current.replace(old_text, new_text, 1)
    await _atomic_write(target_path, replaced)

    return MemoryWriteResult(
        ok=True,
        status="written",
        message="content replaced",
        target=action.target,
        path=str(target_path),
        chars=len(replaced),
    )


async def _do_remove(
    target_path: Path,
    action: MemoryWriteAction,
    current: str | None,
) -> MemoryWriteResult:
    """执行 remove 操作。"""
    text = action.text or ""

    if not text:
        return MemoryWriteResult(
            ok=False,
            status="error",
            message="remove action requires text",
            target=action.target,
            path=str(target_path),
        )

    if current is None or text not in current:
        return MemoryWriteResult(
            ok=False,
            status="not_found",
            message="text not found in target file",
            target=action.target,
            path=str(target_path),
        )

    removed = current.replace(text, "", 1)
    # 清理 3+ 连续换行为 2 个（一次到位，不依赖多轮 replace）
    removed = re.sub(r"\n{3,}", "\n\n", removed).strip() + "\n"
    await _atomic_write(target_path, removed)

    return MemoryWriteResult(
        ok=True,
        status="written",
        message="content removed",
        target=action.target,
        path=str(target_path),
        chars=len(removed),
    )


__all__ = [
    "execute_write",
    "scan_content",
]
