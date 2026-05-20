"""JSONL 持久化历史 parser（v0.2）。

读 ``~/.claude/projects/<encoded-cwd>/<claude_thread_id>.jsonl``，把 Claude Code
SDK 落盘的原始 entry 流翻译成 :class:`NormalizedMessage` 形态的 dict 列表，供
``/api/projects/<cwd>/sessions/<sid>/history`` 这类 history-resume 接口直接吐
回前端。

设计要点（钉死，跟 v0.2 plan.md 接口契约对齐）：

- 与 :mod:`web.claude_code.normalizer`（live 流翻译器）**独立实现**——live
  消费 SDK 类型实例，本 parser 消费纯 JSON dict，复用没价值
- 模块只 import 标准库 + :mod:`web.llm_protocol`（拿 :class:`NormalizedMessage`
  类型定义） + :mod:`web.claude_code._content_filter`（共享前缀过滤规则，
  保证 live 与 history 对 CLI 注入的过滤行为对齐）；**不能**
  import ``claude_agent_sdk``——历史 parser 不依赖 SDK 运行时
- 纯函数：同输入同输出，不带全局状态
- 流式读：line by line，**不** ``readlines`` / ``readall``，避免大文件 OOM
- 行级容错：单行 JSON 解析失败 / 关键字段缺失 / ``sessionId`` 不匹配 →
  ``skip`` 不抛
- 不写 stdout；解析异常用 :func:`logging.getLogger` ``warning`` 级别记录

支持的 entry type（其余全 skip）：

============================  =============================================
JSONL entry                   NormalizedMessage kind
----------------------------  ---------------------------------------------
``type=system, subtype=init`` ``session_created`` + ``newSessionId``
``type=user (str content)``   ``text`` (role=user)
``type=user (tool_result)``   ``tool_result``（遍历 content list 内每个 block）
``type=assistant (text)``     ``text``（role=assistant）
``type=assistant (thinking)`` ``thinking``
``type=assistant (tool_use)`` ``tool_use``
其他（attachment / queue-operation / file-history-snapshot / permission-mode /
last-prompt 等）                丢弃
============================  =============================================

每条输出统一附 ``sessionId`` / ``provider="claude"`` / ``timestamp`` /
``id``（来自 ``entry.uuid``）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from web.claude_code._content_filter import is_internal_content

if TYPE_CHECKING:
    # 仅用于类型注释——返回 dict 与 NormalizedMessage TypedDict 形态对齐。
    from web.llm_protocol import NormalizedMessage  # noqa: F401

__all__ = ["encode_cwd", "jsonl_path_for", "parse_jsonl_history"]

_logger = logging.getLogger(__name__)


def jsonl_path_for(
    cwd: str,
    claude_thread_id: str,
    claude_home: Path | None = None,
) -> Path:
    """根据 cwd + claude_thread_id 拼出 jsonl 文件路径。

    Args:
        cwd: 工作目录原文（如 ``/Volumes/machub_app/proj/kongming-agent``）。
            SDK 编码规则把 ``/`` / ``_`` / ``.`` 都换成 ``-``（与 SDK 子进程
            落盘时一致；e2e 实测 ``/Volumes/machub_app/proj/kongming-agent``
            对应目录名 ``-Volumes-machub-app-proj-kongming-agent``，注意
            ``_`` 也变 ``-``）。
        claude_thread_id: SDK session id（jsonl 文件名去掉 ``.jsonl`` 后缀）。
        claude_home: Claude 数据目录，默认 ``Path.home() / ".claude"``。
            单元测试可注入临时目录。

    Returns:
        ``<claude_home>/projects/<encoded-cwd>/<claude_thread_id>.jsonl``。
        **不校验文件存在**，只做路径拼接。
    """
    home = claude_home if claude_home is not None else Path.home() / ".claude"
    encoded = encode_cwd(cwd)
    return home / "projects" / encoded / f"{claude_thread_id}.jsonl"


def encode_cwd(cwd: str) -> str:
    """SDK 编码 cwd 为 ``~/.claude/projects/`` 下的目录名。

    实测 SDK 规则（从已落盘目录名反推）：``/`` / ``_`` / ``.`` 都换成 ``-``，
    其余字符（字母数字 + ``-``）保留。
    """
    return cwd.replace("/", "-").replace("_", "-").replace(".", "-")


def parse_jsonl_history(
    path: Path,
    claude_thread_id: str,
) -> list[dict[str, Any]]:
    """读取 jsonl 文件，过滤 sessionId 匹配的行，翻译成 NormalizedMessage 列表。

    Args:
        path: jsonl 文件路径。文件不存在 / 无权限 → 返回空列表。
        claude_thread_id: 期望的 SDK session id。``entry.sessionId`` 不匹配
            的行直接 skip。

    Returns:
        :class:`NormalizedMessage` 形态的 dict 列表，按 ``timestamp`` 升序
        排序（ISO 字符串字典序天然就是时间序）。
    """
    messages: list[dict[str, Any]] = []

    try:
        fh = path.open("r", encoding="utf-8")
    except OSError as exc:
        _logger.warning("jsonl_history: cannot open %s: %s", path, exc)
        return messages

    try:
        with fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, json.JSONDecodeError) as exc:
                    _logger.warning("jsonl_history: skip malformed line in %s: %s", path, exc)
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("sessionId") != claude_thread_id:
                    continue

                translated = _translate_entry(entry, claude_thread_id)
                messages.extend(translated)
    except OSError as exc:
        _logger.warning("jsonl_history: read error on %s: %s", path, exc)
        return messages

    messages.sort(key=lambda m: m.get("timestamp") or "")
    return messages


# ---------------------------------------------------------------------------
# 内部翻译
# ---------------------------------------------------------------------------


def _translate_entry(
    entry: dict[str, Any],
    claude_thread_id: str,
) -> list[dict[str, Any]]:
    """把单条 entry 翻译成 0..N 条 NormalizedMessage dict。

    未知 / 不感兴趣的 type 直接返回空列表。
    """
    entry_type = entry.get("type")

    if entry_type == "system":
        if entry.get("subtype") != "init":
            return []
        out = _base(entry, claude_thread_id)
        out["kind"] = "session_created"
        new_sid = entry.get("sessionId")
        if isinstance(new_sid, str):
            out["newSessionId"] = new_sid
        return [out]

    if entry_type == "user":
        return _translate_user(entry, claude_thread_id)

    if entry_type == "assistant":
        return _translate_assistant(entry, claude_thread_id)

    return []


def _translate_user(
    entry: dict[str, Any],
    claude_thread_id: str,
) -> list[dict[str, Any]]:
    """``type=user`` → ``text`` (str) 或 ``tool_result``（list of blocks）。"""
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")

    if isinstance(content, str):
        # CLI 注入的 <task-notification> 等系统通知伪装成 UserMessage，
        # 过滤掉避免历史回放后渲染成右侧用户气泡（live 流在 normalizer
        # _handle_user 走 return [] 已隔离；此处对齐 history 路径）
        if is_internal_content(content):
            return []
        out = _base(entry, claude_thread_id)
        out["kind"] = "text"
        out["role"] = "user"
        out["content"] = content
        return [out]

    if isinstance(content, list):
        results: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str):
                continue
            out = _base(entry, claude_thread_id)
            out["kind"] = "tool_result"
            out["toolId"] = tool_use_id
            if "content" in block:
                out["content"] = block.get("content")
            is_error = block.get("is_error")
            if isinstance(is_error, bool):
                out["isError"] = is_error
            results.append(out)
        return results

    return []


def _translate_assistant(
    entry: dict[str, Any],
    claude_thread_id: str,
) -> list[dict[str, Any]]:
    """``type=assistant`` → 遍历 ``message.content`` blocks，按 block.type 翻译。"""
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []

    results: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")

        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                continue
            out = _base(entry, claude_thread_id)
            out["kind"] = "text"
            out["role"] = "assistant"
            out["content"] = text
            results.append(out)

        elif block_type == "thinking":
            thinking = block.get("thinking")
            if not isinstance(thinking, str):
                continue
            out = _base(entry, claude_thread_id)
            out["kind"] = "thinking"
            out["content"] = thinking
            results.append(out)

        elif block_type == "tool_use":
            tool_id = block.get("id")
            tool_name = block.get("name")
            if not isinstance(tool_id, str) or not isinstance(tool_name, str):
                continue
            out = _base(entry, claude_thread_id)
            out["kind"] = "tool_use"
            out["toolId"] = tool_id
            out["toolName"] = tool_name
            out["toolInput"] = block.get("input")
            results.append(out)

        # 其他 block.type 跳过

    return results


def _base(
    entry: dict[str, Any],
    claude_thread_id: str,
) -> dict[str, Any]:
    """构造 NormalizedMessage 的 base 字段（id / sessionId / timestamp / provider）。

    ``timestamp`` 取自 ``entry.timestamp``（ISO 字符串原样保留）；缺失时设
    为空串以保证排序稳定。``id`` 取自 ``entry.uuid``；缺失时设为空串。
    """
    base: dict[str, Any] = {
        "sessionId": claude_thread_id,
        "provider": "claude",
    }
    timestamp = entry.get("timestamp")
    base["timestamp"] = timestamp if isinstance(timestamp, str) else ""
    uuid = entry.get("uuid")
    if isinstance(uuid, str):
        base["id"] = uuid
    else:
        base["id"] = ""
    return base
