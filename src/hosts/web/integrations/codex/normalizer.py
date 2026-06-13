"""Codex stdout jsonl 事件 → :class:`NormalizedMessage` 归一化器（v0.1）。

输入两源（同一套映射，docs/codex-web-integration-v0.1/02-protocol.md §2.1）：

- 实时：``codex exec --json`` stdout 行（``service.py`` 喂入）
- 历史：``rollout-*.jsonl`` 中 ``event_msg.payload``（``jsonl_history.py`` 喂入）

设计要点：

- 协议宽松：unknown 事件走 raw_dump 不抛错（schema 演进容忍）
- 内部块（``<system-reminder>`` / ``<environment_details>`` 等）从 text/thinking
  剥离后再下发——参考 ``src/hosts/web/integrations/claude_code/normalizer.py`` 同款 prefixes 列表
- 一个事件可能产出 0..N 条出站消息（如 ``command_execution`` 拆 ``tool_use``
  / ``tool_result``；``file_change`` / ``mcp_tool_call`` 单事件出双消息）
- 函数式（无内部状态），与 :class:`ClaudeNormalizer` 不同——codex v0.1 没有
  ApprovalBridge 协调的 deny 去重，也没有跨事件 message_id 锚点

事件映射表（13 行 + 兜底，详见 02-protocol.md §2.1）：

============================  =====================  =======================================
codex event                   item.type              NormalizedMessage.frame_type
----------------------------  ---------------------  ---------------------------------------
``thread.started``            —                      ``session_created``
``turn.started``              —                      *（不发出，砍）*
``turn.completed``            —                      ``complete`` + ``tokenBudget``
``turn.failed``               —                      ``error``
``item.completed``            ``agent_message``      ``text`` (role=assistant)
``item.completed``            ``reasoning``          ``thinking``
``item.started``              ``command_execution``  ``tool_use`` (toolName="shell")
``item.completed``            ``command_execution``  ``tool_result``
``item.completed``            ``file_change``        ``tool_use`` + ``tool_result``
``item.completed``            ``mcp_tool_call``      ``tool_use`` + ``tool_result``
``item.completed``            ``web_search``         ``tool_use``
``item.completed``            ``todo_list``          ``status``
``item.completed``            ``error``              ``error``
``*`` (unknown)               —                      ``error`` (含 raw)
============================  =====================  =======================================
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from hosts.web.app_support.llm_protocol import MessageKind, NormalizedMessage

# ---------------------------------------------------------------------------
# 内部 system 文本前缀（不泄漏给前端）
# ---------------------------------------------------------------------------

INTERNAL_CONTENT_PREFIXES: tuple[str, ...] = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<system-reminder>",
    "<environment_details>",
    "Caveat:",
    "This session is being continued from a previous",
    "[Request interrupted",
)
"""命中任一前缀的整段文本会被剥离。

参考 ``src/hosts/web/integrations/claude_code/normalizer.py:56-65`` 同款常量。codex 实测会注入
``<system-reminder>`` / ``<environment_details>`` 块，与 claude 同源。
"""

# 匹配整段成对块（如 `<system-reminder>...</system-reminder>`，多行 / 贪婪）
_PAIRED_BLOCK_RE = re.compile(
    r"<(system-reminder|environment_details|command-name|command-message|command-args|local-command-stdout)>"
    r".*?</\1>",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO8601 UTC 时间戳。"""
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    """UUID v4。"""
    return str(uuid.uuid4())


def _base(session_id: str | None, frame_type: MessageKind) -> NormalizedMessage:
    """所有出站消息共享的 base 字段。"""
    out: NormalizedMessage = {
        "id": _new_id(),
        "sessionId": session_id,
        "timestamp": _now_iso(),
        "provider": "codex",
        "frame_type": frame_type,
    }
    return out


def _strip_internal(text: str) -> str:
    """剥离 ``<system-reminder>...</system-reminder>`` 等内部块 + 前缀整条。

    两步：

    1. ``re.sub`` 去掉成对块（即使在文本中段也能切除）
    2. 对剩余首段命中 :data:`INTERNAL_CONTENT_PREFIXES` 的整条丢弃（返回空串）
    """
    if not text:
        return text
    cleaned = _PAIRED_BLOCK_RE.sub("", text).strip()
    if not cleaned:
        return ""
    if any(cleaned.startswith(p) for p in INTERNAL_CONTENT_PREFIXES):
        return ""
    return cleaned


# ---------------------------------------------------------------------------
# 主分发入口
# ---------------------------------------------------------------------------


def normalize(event: dict[str, Any], session_id: str | None) -> list[NormalizedMessage]:
    """codex jsonl event → 0..N 条 :class:`NormalizedMessage`。

    输入：单行 jsonl 解析后的 dict（实时 stdout 行 / rollout payload 同源）。
    """
    event_type = event.get("type", "")
    if event_type == "thread.started":
        return [_handle_thread_started(event, session_id)]
    if event_type == "turn.started":
        # v0.1 不发出，前端不需要"思考中"占位
        return []
    if event_type == "turn.completed":
        return [_handle_turn_completed(event, session_id)]
    if event_type == "turn.failed":
        return [_handle_turn_failed(event, session_id)]
    if event_type in ("item.started", "item.completed", "item.updated"):
        return _handle_item(event, session_id)
    return [_unknown_event(event, session_id)]


# ---------------------------------------------------------------------------
# 顶层事件 handler
# ---------------------------------------------------------------------------


def _handle_thread_started(event: dict[str, Any], session_id: str | None) -> NormalizedMessage:
    thread_id = event.get("thread_id") or session_id
    out = _base(thread_id, "session_created")
    if thread_id is not None:
        out["newSessionId"] = thread_id
    return out


def _handle_turn_completed(event: dict[str, Any], session_id: str | None) -> NormalizedMessage:
    out = _base(session_id, "complete")
    out["exitCode"] = 0
    usage = _normalize_usage(event.get("usage"))
    if usage:
        out["tokenBudget"] = usage
    return out


def _handle_turn_failed(event: dict[str, Any], session_id: str | None) -> NormalizedMessage:
    out = _base(session_id, "error")
    err = event.get("error") or {}
    if isinstance(err, dict):
        message = err.get("message") or err.get("type") or "turn failed"
    else:
        message = str(err)
    out["error"] = message
    return out


def _handle_item(event: dict[str, Any], session_id: str | None) -> list[NormalizedMessage]:
    """``item.{started,completed,updated}`` 二级分发到 item.type。"""
    event_type = event.get("type", "")
    item = event.get("item") or {}
    if not isinstance(item, dict):
        return [_unknown_event(event, session_id)]
    item_type = item.get("type", "")

    # v0.1 codex 不发增量 item.updated（即使发了也忽略）
    if event_type == "item.updated":
        return []

    is_started = event_type == "item.started"

    if item_type == "agent_message":
        return _handle_agent_message(item, session_id) if not is_started else []
    if item_type == "reasoning":
        return _handle_reasoning(item, session_id) if not is_started else []
    if item_type == "command_execution":
        return _handle_command_execution(item, session_id, is_started=is_started)
    if item_type == "file_change":
        return _handle_file_change(item, session_id) if not is_started else []
    if item_type == "mcp_tool_call":
        return _handle_mcp_tool_call(item, session_id) if not is_started else []
    if item_type == "web_search":
        return _handle_web_search(item, session_id) if not is_started else []
    if item_type == "todo_list":
        return _handle_todo_list(item, session_id) if not is_started else []
    if item_type == "error":
        return _handle_item_error(item, session_id) if not is_started else []
    return [_unknown_event(event, session_id)]


# ---------------------------------------------------------------------------
# item.type handler
# ---------------------------------------------------------------------------


def _handle_agent_message(item: dict[str, Any], session_id: str | None) -> list[NormalizedMessage]:
    text = _strip_internal(str(item.get("text", "")))
    if not text:
        return []
    out = _base(session_id, "text")
    out["role"] = "assistant"
    out["content"] = text
    return [out]


def _handle_reasoning(item: dict[str, Any], session_id: str | None) -> list[NormalizedMessage]:
    # codex reasoning 字段有 text / summary 两种实测命名，宽松接
    raw = item.get("text") or item.get("summary") or ""
    text = _strip_internal(str(raw))
    if not text:
        return []
    out = _base(session_id, "thinking")
    out["role"] = "assistant"
    out["content"] = text
    return [out]


def _handle_command_execution(
    item: dict[str, Any],
    session_id: str | None,
    *,
    is_started: bool,
) -> list[NormalizedMessage]:
    """command_execution 两阶段：started → tool_use；completed → tool_result。"""
    item_id = str(item.get("id", "")) or _new_id()
    if is_started:
        out = _base(session_id, "tool_use")
        out["toolId"] = item_id
        out["toolName"] = "shell"
        out["toolInput"] = {"command": item.get("command", "")}
        return [out]

    # completed
    out = _base(session_id, "tool_result")
    out["toolId"] = item_id
    out["content"] = item.get("aggregated_output", "")
    exit_code = item.get("exit_code")
    out["isError"] = exit_code is not None and exit_code != 0
    return [out]


def _handle_file_change(item: dict[str, Any], session_id: str | None) -> list[NormalizedMessage]:
    """file_change 单事件出双消息（tool_use + tool_result）。"""
    item_id = str(item.get("id", "")) or _new_id()
    changes = item.get("changes")
    status = item.get("status", "")

    use = _base(session_id, "tool_use")
    use["toolId"] = item_id
    use["toolName"] = "edit"
    use["toolInput"] = {"changes": changes} if changes is not None else {}

    result = _base(session_id, "tool_result")
    result["toolId"] = item_id
    result["content"] = changes if changes is not None else ""
    result["isError"] = status != "completed"
    return [use, result]


def _handle_mcp_tool_call(item: dict[str, Any], session_id: str | None) -> list[NormalizedMessage]:
    """mcp_tool_call 单事件出双消息。"""
    item_id = str(item.get("id", "")) or _new_id()
    server = item.get("server", "")
    tool = item.get("tool", "")
    tool_name = f"{server}/{tool}" if server or tool else "mcp"

    use = _base(session_id, "tool_use")
    use["toolId"] = item_id
    use["toolName"] = tool_name
    use["toolInput"] = item.get("arguments", {})

    result = _base(session_id, "tool_result")
    result["toolId"] = item_id
    if "result" in item:
        result["content"] = item.get("result")
    elif "output" in item:
        result["content"] = item.get("output")
    err = item.get("error")
    result["isError"] = err is not None
    if err is not None:
        # 错误时 content 优先放 error 信息（前端渲染友好）
        result["content"] = err
    return [use, result]


def _handle_web_search(item: dict[str, Any], session_id: str | None) -> list[NormalizedMessage]:
    item_id = str(item.get("id", "")) or _new_id()
    out = _base(session_id, "tool_use")
    out["toolId"] = item_id
    out["toolName"] = "web_search"
    out["toolInput"] = {"query": item.get("query", "")}
    return [out]


def _handle_todo_list(item: dict[str, Any], session_id: str | None) -> list[NormalizedMessage]:
    out = _base(session_id, "status")
    out["content"] = item.get("items", [])
    return [out]


def _handle_item_error(item: dict[str, Any], session_id: str | None) -> list[NormalizedMessage]:
    out = _base(session_id, "error")
    out["error"] = str(item.get("message") or item.get("error") or "codex item error")
    return [out]


# ---------------------------------------------------------------------------
# 兜底
# ---------------------------------------------------------------------------


def _unknown_event(event: dict[str, Any], session_id: str | None) -> NormalizedMessage:
    """unknown 事件：不抛错，返回 frame_type=error 包裹原 event（schema 演进容忍）。"""
    out = _base(session_id, "error")
    out["error"] = f"unknown codex event type: {event.get('type')!r}"
    out["content"] = {"raw": event}
    return out


# ---------------------------------------------------------------------------
# usage 字段归一化（snake_case → camelCase，对齐 ClaudeNormalizer.tokenBudget）
# ---------------------------------------------------------------------------


def _normalize_usage(usage: Any) -> dict[str, Any]:
    """codex ``turn.completed.usage`` → camelCase tokenBudget。

    codex 实测字段（参考 docs/codex-web-integration-v0.1/02-protocol.md §2.1）：

    - ``input_tokens`` / ``cached_input_tokens`` / ``output_tokens``
    - ``reasoning_output_tokens``

    未知字段走自动 snake_to_camel（避免 SDK 升级混搭）。
    """
    if not isinstance(usage, dict):
        return {}
    key_map: dict[str, str] = {
        "input_tokens": "inputTokens",
        "cached_input_tokens": "cacheReadTokens",
        "output_tokens": "outputTokens",
        "reasoning_output_tokens": "reasoningOutputTokens",
    }
    out: dict[str, Any] = {}
    raw = cast(dict[str, Any], usage)
    for key, value in raw.items():
        mapped = key_map.get(key)
        if mapped is None:
            mapped = _snake_to_camel(key) if "_" in key else key
        if mapped not in out:
            out[mapped] = value
    return out


def _snake_to_camel(s: str) -> str:
    """``cached_input_tokens`` → ``cachedInputTokens``。空段安全。"""
    parts = s.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:] if p)


__all__ = ["INTERNAL_CONTENT_PREFIXES", "normalize"]
