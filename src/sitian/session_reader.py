"""
司天 session 逆序读取器——从 jsonl 尾部流式取最近消息，纯 stdlib。

Role:
    从 Claude / Codex 会话 jsonl 末尾取最近若干条 user / assistant 消息文本，
    供扫描器填充 recent_activity。jsonl 可能上百 MB，必须反向分块读取，
    不允许全量 load。

Owns:
    - _ReverseLineIterator（从文件尾部逐块读，O(1) 内存）
    - read_claude_session_tail() / read_kongming_session_tail()（取最近 N 条消息文本）

Does not own:
    - 观察记录生成（scanners.py）
    - session 列表发现（global_scanner.py）
    - 任何持久化操作

Called by:
    - scanners.py（claude_project 扫描时读取最近活动消息）

Key outputs:
    - read_claude_session_tail(path, max_chars) → dict[str, list[str]]
    - read_kongming_session_tail(path, max_chars) → dict[str, list[str]]

Change risks:
    - jsonl 行格式变化会破坏消息提取
    - 零外部依赖约束：不能引入 sitian 兄弟模块
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["read_claude_session_tail", "read_kongming_session_tail"]

_log = logging.getLogger("sitian.session_reader")

_CHUNK_SIZE = 2048
_ELLIPSIS = "…"
_MAX_LINE_BYTES = 2_097_152


def read_claude_session_tail(
    jsonl_path: Path,
    *,
    user_count: int = 1,
    assistant_count: int = 1,
    max_chars: int = 500,
) -> dict[str, list[str]]:
    """反向读 jsonl，收集最后 N 条 user 消息 + M 条 assistant 消息。

    返回 ``{"users": [...], "assistants": [...]}``，数组从新到旧排序
    （索引 0 = 最新一条）。长度 ≤ 对应 count；不足则尽收。
    每条按 ``max_chars`` 截断（超出末尾加单字符省略号 ``…``）；
    ``max_chars=0`` 表示不截断；对应 count=0 时数组为空。

    文件不存在、OSError、JSON 解析错误均静默跳过，不抛异常。
    """

    return _read_session_tail(
        jsonl_path,
        user_count=user_count,
        assistant_count=assistant_count,
        max_chars=max_chars,
        role_for_entry=_claude_entry_role,
    )


def read_kongming_session_tail(
    jsonl_path: Path,
    *,
    user_count: int = 1,
    assistant_count: int = 1,
    max_chars: int = 500,
) -> dict[str, list[str]]:
    """反向读取 FileSession JSONL 中最近的 user/assistant 正文。

    只接受 ``record_type="message"`` 的记录，并从内层 ``message.role`` 区分角色。
    返回形态、逆序分块和截断语义与 :func:`read_claude_session_tail` 保持一致。
    """

    return _read_session_tail(
        jsonl_path,
        user_count=user_count,
        assistant_count=assistant_count,
        max_chars=max_chars,
        role_for_entry=_kongming_entry_role,
    )


def _read_session_tail(
    jsonl_path: Path,
    *,
    user_count: int,
    assistant_count: int,
    max_chars: int,
    role_for_entry: Callable[[dict[str, Any]], str | None],
) -> dict[str, list[str]]:
    """使用给定角色提取规则反向读取受限消息尾部。"""

    if user_count < 0 or assistant_count < 0:
        return {"users": [], "assistants": []}
    if user_count == 0 and assistant_count == 0:
        return {"users": [], "assistants": []}

    try:
        resolved = jsonl_path.resolve()
    except (OSError, RuntimeError):
        return {"users": [], "assistants": []}

    users: list[str] = []
    assistants: list[str] = []

    try:
        with contextlib.closing(_iter_lines_reverse(resolved)) as it:
            for line in it:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry: dict[str, Any] = json.loads(stripped)
                except (ValueError, json.JSONDecodeError) as exc:
                    _log.warning("skip corrupted JSON line in %s: %s", resolved, exc)
                    continue
                if not isinstance(entry, dict):
                    continue

                role = role_for_entry(entry)
                if role == "user" and len(users) < user_count:
                    text = _extract_message_text(entry)
                    if text is not None:
                        users.append(_finalize_text(text, max_chars))
                elif role == "assistant" and len(assistants) < assistant_count:
                    text = _extract_message_text(entry)
                    if text is not None:
                        assistants.append(_finalize_text(text, max_chars))

                if len(users) >= user_count and len(assistants) >= assistant_count:
                    break
    except OSError as exc:
        _log.warning("failed to read session file %s: %s", resolved, exc)
        return {"users": [], "assistants": []}

    return {"users": users, "assistants": assistants}


def _claude_entry_role(entry: dict[str, Any]) -> str | None:
    """返回 Claude transcript entry 的 user/assistant 角色。"""

    entry_type = entry.get("type")
    return entry_type if entry_type in {"user", "assistant"} else None


def _kongming_entry_role(entry: dict[str, Any]) -> str | None:
    """返回 FileSession message record 的 user/assistant 角色。"""

    if entry.get("record_type") != "message":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    return role if role in {"user", "assistant"} else None


def _extract_message_text(entry: dict[str, Any]) -> str | None:
    """从一条 user / assistant 记录提取归一化后的纯文本。

    支持两种 content 形态：
      - str：直接使用
      - list[dict]：拼接所有 ``type == "text"`` 元素的 ``text`` 字段
    其他形态返回 None。
    """

    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")

    raw: str
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text_val = item.get("text")
            if isinstance(text_val, str) and text_val:
                parts.append(text_val)
        if not parts:
            return None
        raw = " ".join(parts)
    else:
        return None

    normalized = " ".join(raw.split())
    if not normalized:
        return None
    return normalized


def _finalize_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + _ELLIPSIS


def _iter_lines_reverse(path: Path) -> _ReverseLineIterator:
    """逐行反向迭代文件内容（生成器）。

    实现：从末尾按固定块大小往前 read，按换行切分、保留跨块残片，
    最后逆序 yield。不会全量 load 文件。
    """

    return _ReverseLineIterator(path)


class _ReverseLineIterator:
    """反向行迭代器。

    迭代结束自动关闭底层文件句柄。中途异常时也会在 ``__del__`` 时关闭。
    """

    def __init__(self, path: Path) -> None:
        # 二进制模式：避免文本模式 seek 不可靠。
        self._fh = open(path, "rb")  # noqa: SIM115 — 迭代器持有
        try:
            self._fh.seek(0, os.SEEK_END)
            self._pos = self._fh.tell()
        except OSError:
            self._fh.close()
            raise
        self._buffer = b""
        self._exhausted = False

    def __iter__(self) -> _ReverseLineIterator:
        return self

    def __next__(self) -> str:
        while True:
            if self._exhausted and not self._buffer:
                self.close()
                raise StopIteration

            # 尝试从当前 buffer 切出最后一整行（不含尾部换行）。
            # buffer 内的内容代表"文件中当前未消费区段"，永远从末尾切。
            newline_idx = self._buffer.rfind(b"\n")
            if newline_idx != -1:
                # 末尾换行右侧的内容（不含换行）是最后一行。
                line_bytes = self._buffer[newline_idx + 1 :]
                self._buffer = self._buffer[:newline_idx]
                # 跳过完全空白的行（包括只剩 \r 等）。
                if line_bytes.strip(b"\r\n\t "):
                    return line_bytes.decode("utf-8", errors="replace")
                # 空行：继续 loop 切下一行。
                continue

            if self._exhausted:
                # 已读到文件头但 buffer 还剩内容（无换行 → 即整段就是一行）。
                line_bytes = self._buffer
                self._buffer = b""
                if line_bytes.strip(b"\r\n\t "):
                    return line_bytes.decode("utf-8", errors="replace")
                continue

            # buffer 没换行又没读完，往前再读一块。
            self._read_more()

    def _read_more(self) -> None:
        if self._pos <= 0:
            self._exhausted = True
            return
        read_size = min(_CHUNK_SIZE, self._pos)
        self._pos -= read_size
        self._fh.seek(self._pos, os.SEEK_SET)
        chunk = self._fh.read(read_size)
        self._buffer = chunk + self._buffer
        if self._pos <= 0:
            self._exhausted = True
        if len(self._buffer) > _MAX_LINE_BYTES and b"\n" not in self._buffer:
            _log.warning(
                "skip oversized line (buffer >%d bytes, no newline) during reverse read",
                _MAX_LINE_BYTES,
            )
            self._buffer = b""

    def close(self) -> None:
        if self._fh and not self._fh.closed:
            self._fh.close()

    def __del__(self) -> None:  # pragma: no cover - 防御性
        with contextlib.suppress(Exception):
            self.close()
