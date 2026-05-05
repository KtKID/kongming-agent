"""Codex rollout JSONL 历史 parser（v0.1）。

读 ``~/.codex/sessions/rollout-<session_id>.jsonl``，把 Codex CLI
落盘的 rollout 事件流翻译成 :class:`NormalizedMessage` 形态的 dict 列表，供
``/api/projects/<cwd>/sessions/<sid>/history`` 接口吐回前端。

设计要点（钉死，对齐 codex-web-integration-v0.1 plan）：

- 与 :mod:`web.codex.normalizer`（live 流翻译器）**独立实现**——live
  消费 stdout jsonl 行（``item.completed`` + ``item`` wrapper），本 parser
  消费 rollout ``event_msg.payload``（扁平结构，type 为 ``agent_message``
  / ``token_count`` 等）。字段形态差异大，复用 normalizer 反而需要适配层，
  不如直接翻译更清晰
- 模块只 import 标准库 + :mod:`web.codex.normalizer` 的 strip 工具；
  **不能** import ``claude_code`` 任何模块
- 纯函数：同输入同输出，不带全局状态
- 流式读：line by line，**不** ``readlines`` / ``readall``，避免大文件 OOM
- 行级容错：单行 JSON 解析失败 / 关键字段缺失 → ``skip`` 不抛
- 不写 stdout；解析异常用 :func:`logging.getLogger` ``warning`` 级别记录

支持的 rollout event_msg.payload type（其余 skip）：

============================  =============================================
rollout payload type          NormalizedMessage kind
----------------------------  ---------------------------------------------
``agent_message``             ``text`` (role=assistant)
``reasoning``                 ``thinking``
``command_execution``         ``tool_use`` + ``tool_result``
``file_change``               ``tool_use`` + ``tool_result``
``mcp_tool_call``             ``tool_use`` + ``tool_result``
``web_search``                ``tool_use``
``todo_list``                 ``status``
``error``                     ``error``
``token_count``               ``complete`` + ``tokenBudget``
``task_started``              *跳过（session_meta 已用过 / 回放不需要）*
``task_complete``             *跳过（token_count 已发 complete）*
``user_message``              *跳过（history 不含 user 输入）*
============================  =============================================

每条输出统一附 ``sessionId`` / ``provider="codex"`` / ``timestamp`` /
``id``。
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from web.codex.normalizer import _PAIRED_BLOCK_RE, INTERNAL_CONTENT_PREFIXES

if TYPE_CHECKING:
    from web.llm_protocol import NormalizedMessage  # noqa: F401

__all__ = ["parse_codex_rollout", "read_session_meta"]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def parse_codex_rollout(
    path: Path,
    session_id: str,
) -> list[dict[str, Any]]:
    """读取 codex rollout-*.jsonl，翻译成 NormalizedMessage 列表。

    rollout 文件结构（每行 ``{timestamp, type, payload}``）：

    - ``session_meta``：跳过（``projects_scanner`` 已用过）
    - ``turn_context``：跳过（metadata 已有 model/approval_policy）
    - ``event_msg``：payload 独立翻译为 0..N 条 NormalizedMessage
    - ``response_item``：跳过（OpenAI Responses 内部状态，回放不需要）

    流式逐行读（不 readlines / readall），大文件安全。
    单行 JSON 解析失败 -> skip + logging.warning（不抛）。

    Args:
        path: rollout jsonl 文件路径。文件不存在 / 无权限 -> 返回空列表。
        session_id: 当前 session id，注入到每条输出的 ``sessionId`` 字段。

    Returns:
        :class:`NormalizedMessage` 形态的 dict 列表，按 ``timestamp`` 升序
        排序（ISO 字符串字典序天然就是时间序）。
    """
    messages: list[dict[str, Any]] = []

    try:
        fh = path.open("r", encoding="utf-8")
    except OSError as exc:
        _logger.warning("codex_rollout: cannot open %s: %s", path, exc)
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
                    _logger.warning(
                        "codex_rollout: skip malformed line in %s: %s",
                        path,
                        exc,
                    )
                    continue
                if not isinstance(entry, dict):
                    continue

                entry_type = entry.get("type")
                if entry_type != "event_msg":
                    continue

                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue

                timestamp = entry.get("timestamp", "")
                if not isinstance(timestamp, str):
                    timestamp = ""

                translated = _translate_payload(payload, session_id, timestamp)
                messages.extend(translated)
    except OSError as exc:
        _logger.warning("codex_rollout: read error on %s: %s", path, exc)
        return messages

    messages.sort(key=lambda m: m.get("timestamp") or "")
    return messages


def read_session_meta(path: Path) -> dict[str, Any] | None:
    """只读 rollout 第一行，返回 ``session_meta.payload`` dict。

    用途：``projects_scanner`` 需要从 rollout 第一行提取
    ``id`` / ``cwd`` / ``cli_version``。

    文件不存在 / 非 ``session_meta`` -> ``None``。
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            first_line = f.readline()
    except OSError:
        return None

    if not first_line or not first_line.strip():
        return None

    try:
        entry = json.loads(first_line)
    except (ValueError, json.JSONDecodeError):
        return None

    if not isinstance(entry, dict):
        return None
    if entry.get("type") != "session_meta":
        return None

    payload = entry.get("payload")
    if isinstance(payload, dict):
        return payload
    return None


# ---------------------------------------------------------------------------
# 内部翻译
# ---------------------------------------------------------------------------


def _strip_internal(text: str) -> str:
    """剥离 ``<system-reminder>`` 等内部块 + 前缀整条。

    复用 normalizer 导出的 ``INTERNAL_CONTENT_PREFIXES`` 常量和
    ``_PAIRED_BLOCK_RE`` 正则——避免两处列表漂移。
    """
    if not text:
        return text
    cleaned = _PAIRED_BLOCK_RE.sub("", text).strip()
    if not cleaned:
        return ""
    if any(cleaned.startswith(p) for p in INTERNAL_CONTENT_PREFIXES):
        return ""
    return cleaned


def _new_id() -> str:
    """UUID v4。"""
    return str(uuid.uuid4())


def _base(session_id: str, timestamp: str) -> dict[str, Any]:
    """构造 NormalizedMessage 的 base 字段。

    ``timestamp`` 取自 rollout 行的 ``entry.timestamp``（ISO 字符串原样保留）。
    ``id`` 生成 UUID v4。
    """
    return {
        "id": _new_id(),
        "sessionId": session_id,
        "timestamp": timestamp,
        "provider": "codex",
    }


def _translate_payload(
    payload: dict[str, Any],
    session_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    """把单条 ``event_msg.payload`` 翻译成 0..N 条 NormalizedMessage dict。

    未知 / 不感兴趣的 payload type 直接返回空列表。
    """
    payload_type = payload.get("type", "")

    if payload_type == "agent_message":
        return _handle_agent_message(payload, session_id, timestamp)
    if payload_type == "reasoning":
        return _handle_reasoning(payload, session_id, timestamp)
    if payload_type == "command_execution":
        return _handle_command_execution(payload, session_id, timestamp)
    if payload_type == "file_change":
        return _handle_file_change(payload, session_id, timestamp)
    if payload_type == "mcp_tool_call":
        return _handle_mcp_tool_call(payload, session_id, timestamp)
    if payload_type == "web_search":
        return _handle_web_search(payload, session_id, timestamp)
    if payload_type == "todo_list":
        return _handle_todo_list(payload, session_id, timestamp)
    if payload_type == "error":
        return _handle_error(payload, session_id, timestamp)
    if payload_type == "token_count":
        return _handle_token_count(payload, session_id, timestamp)

    # task_started / task_complete / user_message 等 → 跳过
    return []


# ---------------------------------------------------------------------------
# payload type handler
# ---------------------------------------------------------------------------


def _handle_agent_message(
    payload: dict[str, Any],
    session_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    text = _strip_internal(str(payload.get("text", "")))
    if not text:
        return []
    out = _base(session_id, timestamp)
    out["kind"] = "text"
    out["role"] = "assistant"
    out["content"] = text
    return [out]


def _handle_reasoning(
    payload: dict[str, Any],
    session_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    raw = payload.get("text") or payload.get("summary") or ""
    text = _strip_internal(str(raw))
    if not text:
        return []
    out = _base(session_id, timestamp)
    out["kind"] = "thinking"
    out["role"] = "assistant"
    out["content"] = text
    return [out]


def _handle_command_execution(
    payload: dict[str, Any],
    session_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    """command_execution：拆 tool_use + tool_result 双消息。

    rollout 的 command_execution 是完成态（非 started/completed 两阶段），
    所以一次性出 use + result 两条。
    """
    item_id = str(payload.get("id", "")) or _new_id()

    use = _base(session_id, timestamp)
    use["kind"] = "tool_use"
    use["toolId"] = item_id
    use["toolName"] = "shell"
    use["toolInput"] = {"command": payload.get("command", "")}

    result = _base(session_id, timestamp)
    result["kind"] = "tool_result"
    result["toolId"] = item_id
    result["content"] = payload.get("aggregated_output", "")
    exit_code = payload.get("exit_code")
    result["isError"] = exit_code is not None and exit_code != 0

    return [use, result]


def _handle_file_change(
    payload: dict[str, Any],
    session_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    """file_change：拆 tool_use + tool_result 双消息。"""
    item_id = str(payload.get("id", "")) or _new_id()
    changes = payload.get("changes")
    status = payload.get("status", "")

    use = _base(session_id, timestamp)
    use["kind"] = "tool_use"
    use["toolId"] = item_id
    use["toolName"] = "edit"
    use["toolInput"] = {"changes": changes} if changes is not None else {}

    result = _base(session_id, timestamp)
    result["kind"] = "tool_result"
    result["toolId"] = item_id
    result["content"] = changes if changes is not None else ""
    result["isError"] = status != "completed"

    return [use, result]


def _handle_mcp_tool_call(
    payload: dict[str, Any],
    session_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    """mcp_tool_call：拆 tool_use + tool_result 双消息。"""
    item_id = str(payload.get("id", "")) or _new_id()
    server = payload.get("server", "")
    tool = payload.get("tool", "")
    tool_name = f"{server}/{tool}" if server or tool else "mcp"

    use = _base(session_id, timestamp)
    use["kind"] = "tool_use"
    use["toolId"] = item_id
    use["toolName"] = tool_name
    use["toolInput"] = payload.get("arguments", {})

    result = _base(session_id, timestamp)
    result["kind"] = "tool_result"
    result["toolId"] = item_id
    if "result" in payload:
        result["content"] = payload.get("result")
    elif "output" in payload:
        result["content"] = payload.get("output")
    err = payload.get("error")
    result["isError"] = err is not None
    if err is not None:
        result["content"] = err

    return [use, result]


def _handle_web_search(
    payload: dict[str, Any],
    session_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    item_id = str(payload.get("id", "")) or _new_id()
    out = _base(session_id, timestamp)
    out["kind"] = "tool_use"
    out["toolId"] = item_id
    out["toolName"] = "web_search"
    out["toolInput"] = {"query": payload.get("query", "")}
    return [out]


def _handle_todo_list(
    payload: dict[str, Any],
    session_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    out = _base(session_id, timestamp)
    out["kind"] = "status"
    out["content"] = payload.get("items", [])
    return [out]


def _handle_error(
    payload: dict[str, Any],
    session_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    out = _base(session_id, timestamp)
    out["kind"] = "error"
    out["error"] = str(payload.get("message") or payload.get("error") or "codex rollout error")
    return [out]


def _handle_token_count(
    payload: dict[str, Any],
    session_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    """token_count → ``complete`` + ``tokenBudget``。

    rollout ``token_count`` 含 ``usage`` dict（snake_case 字段），
    归一化为 camelCase 对齐前端。
    """
    out = _base(session_id, timestamp)
    out["kind"] = "complete"
    out["exitCode"] = 0
    usage = _normalize_usage(payload.get("usage"))
    if usage:
        out["tokenBudget"] = usage
    return [out]


# ---------------------------------------------------------------------------
# usage 归一化（snake_case → camelCase，对齐前端 tokenBudget）
# ---------------------------------------------------------------------------


def _normalize_usage(usage: Any) -> dict[str, Any]:
    """codex ``token_count.usage`` → camelCase tokenBudget。

    字段映射与 :func:`web.codex.normalizer._normalize_usage` 一致。
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
    for key, value in usage.items():
        mapped = key_map.get(key)
        if mapped is None:
            mapped = _snake_to_camel(key) if "_" in key else key
        if mapped not in out:
            out[mapped] = value
    return out


def _snake_to_camel(s: str) -> str:
    """``cached_input_tokens`` -> ``cachedInputTokens``。"""
    parts = s.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:] if p)
