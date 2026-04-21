"""最小文件 builtin tool 集。

v1-mini 里需要三个最基本的文件工具，让模型能完成"读 → 改 → 看目录"
这条闭环，仅此而已：

- :class:`ReadFileTool`
- :class:`WriteFileTool`
- :class:`ListDirTool`

安全性边界：

- 这一层**不做**路径白名单 / 路径逃逸防护 / 写权限过滤。这些安全策略由
  :mod:`safety.capability_policy` 和 :mod:`safety.permission_policy` 在装配层
  串到 :class:`core.contracts.ApprovalProvider` 前面去做；tool 本身只专注于
  "能不能把这件事做对"，不兼职安全审判。
- 路径统一 :meth:`pathlib.Path.resolve` 后落成绝对路径返回，方便上游审计。

这些工具遵守 :class:`tools.base.BaseBuiltinTool` 的约定：抛出任何异常都会被
基类转成 ``ToolResult(ok=False, error_message=...)``，runner 不会拿到裸异常。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.contracts import Tool, ToolContext
from tools.base import BaseBuiltinTool

# 默认读取上限：64KiB，超过截断并在 content 里明确标注。
# 这不是安全限制（安全归 safety 层），只是避免一次 tool call 把模型上下文打爆。
_DEFAULT_READ_MAX_BYTES = 64 * 1024


class ReadFileTool(BaseBuiltinTool):
    """读取单个文本文件。

    参数：

    - ``path`` (必填)：要读取的文件路径；会被 :meth:`Path.resolve` 解析成绝对路径。
    - ``max_bytes`` (可选)：读取上限，默认取构造参数 ``default_max_bytes``；超过则截断。

    返回 ``content`` 是文件文本本体；``data`` 结构化字段包含：

    - ``path``：resolve 后的绝对路径字符串
    - ``bytes_read``：实际返回的字符字节数（UTF-8 编码长度）
    - ``truncated``：是否因 ``max_bytes`` 截断
    - ``total_bytes``：原始文件字节数
    """

    name = "read_file"
    description = "Read a UTF-8 text file from disk and return its content."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read. May be absolute or relative.",
            },
            "max_bytes": {
                "type": "integer",
                "description": "Maximum bytes to read. Content longer than this is truncated.",
            },
        },
        "required": ["path"],
    }

    def __init__(self, *, default_max_bytes: int = _DEFAULT_READ_MAX_BYTES) -> None:
        self._default_max_bytes = default_max_bytes

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        raw_path = args["path"]
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("'path' must be a non-empty string")

        max_bytes = args.get("max_bytes", self._default_max_bytes)
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("'max_bytes' must be a positive integer if provided")

        abs_path = Path(raw_path).expanduser().resolve()
        if not abs_path.exists():
            raise FileNotFoundError(f"file not found: {abs_path}")
        if not abs_path.is_file():
            raise IsADirectoryError(f"not a regular file: {abs_path}")

        raw_bytes = abs_path.read_bytes()
        total_bytes = len(raw_bytes)
        truncated = total_bytes > max_bytes
        payload = raw_bytes[:max_bytes] if truncated else raw_bytes
        text = payload.decode("utf-8", errors="replace")

        if truncated:
            text += (
                f"\n\n[truncated: returned {max_bytes} of {total_bytes} bytes; "
                "raise max_bytes to read more]"
            )

        data = {
            "path": str(abs_path),
            "bytes_read": len(payload),
            "total_bytes": total_bytes,
            "truncated": truncated,
        }
        return text, data


class WriteFileTool(BaseBuiltinTool):
    """写入 / 追加一个文本文件。

    参数：

    - ``path`` (必填)：目标路径，支持 ``~`` 展开；父目录不存在时自动创建。
    - ``content`` (必填)：要写入的 UTF-8 文本。
    - ``append`` (可选，默认 ``False``)：``True`` 表示追加而不是覆盖。

    返回 ``content`` 是面向模型的简短回执文本；``data`` 结构化字段包含：

    - ``path``：resolve 后的绝对路径字符串
    - ``bytes``：写入的 UTF-8 字节数
    - ``mode``：``"write"`` 或 ``"append"``
    """

    name = "write_file"
    description = (
        "Write or append UTF-8 text content to a file. "
        "Parent directories are created automatically."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Destination file path.",
            },
            "content": {
                "type": "string",
                "description": "UTF-8 text content to write.",
            },
            "append": {
                "type": "boolean",
                "description": "If true, append to the file; otherwise overwrite.",
            },
        },
        "required": ["path", "content"],
    }

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        raw_path = args["path"]
        content = args["content"]
        append = bool(args.get("append", False))

        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("'path' must be a non-empty string")
        if not isinstance(content, str):
            raise ValueError("'content' must be a string")

        abs_path = Path(raw_path).expanduser().resolve()
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        encoded = content.encode("utf-8")
        if append:
            with abs_path.open("ab") as fp:
                fp.write(encoded)
            mode = "append"
        else:
            abs_path.write_bytes(encoded)
            mode = "write"

        data = {
            "path": str(abs_path),
            "bytes": len(encoded),
            "mode": mode,
        }
        summary = f"{mode} {len(encoded)} bytes -> {abs_path}"
        return summary, data


class ListDirTool(BaseBuiltinTool):
    """列出一个目录下的直接子项。

    参数：

    - ``path`` (必填)：目标目录；支持 ``~`` 展开。

    返回 ``content`` 是一段 JSON，``data`` 同样结构化给上游：

    - ``path``：resolve 后的绝对路径
    - ``entries``：``[{"name": str, "type": "file" | "dir" | "other", "size": int}]``

    "other" 覆盖 symlink / socket / fifo 等；``size`` 对于目录和非常规文件
    仍会填一个来自 ``stat().st_size`` 的数字，并不做特别语义化。
    """

    name = "list_dir"
    description = "List direct entries (files and directories) under a given directory."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory to list.",
            },
        },
        "required": ["path"],
    }

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        raw_path = args["path"]
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("'path' must be a non-empty string")

        abs_path = Path(raw_path).expanduser().resolve()
        if not abs_path.exists():
            raise FileNotFoundError(f"path not found: {abs_path}")
        if not abs_path.is_dir():
            raise NotADirectoryError(f"not a directory: {abs_path}")

        entries: list[dict[str, Any]] = []
        for child in sorted(abs_path.iterdir(), key=lambda p: p.name):
            try:
                stat_result = child.stat()
                size = stat_result.st_size
            except OSError:
                size = 0

            if child.is_dir():
                kind = "dir"
            elif child.is_file():
                kind = "file"
            else:
                kind = "other"

            entries.append({"name": child.name, "type": kind, "size": size})

        data = {"path": str(abs_path), "entries": entries}
        # 给模型看的 content：直接 JSON，避免再造一层格式化。
        content_text = json.dumps(data, ensure_ascii=False, indent=2)
        return content_text, data


def build_file_tools(
    enabled: bool = True,
    *,
    read_max_bytes: int = _DEFAULT_READ_MAX_BYTES,
) -> list[Tool]:
    """构造 v1-mini 第一批文件工具。

    Args:
        enabled: ``False`` 时返回空列表，对应 ``config.tool.file.enabled = false``。
        read_max_bytes: ReadFileTool 默认读取上限字节数。

    Returns:
        可以直接塞进 :class:`tools.registry.ToolRegistry` 的工具列表。
    """
    if not enabled:
        return []
    return [ReadFileTool(default_max_bytes=read_max_bytes), WriteFileTool(), ListDirTool()]


__all__ = [
    "ListDirTool",
    "ReadFileTool",
    "WriteFileTool",
    "build_file_tools",
]
