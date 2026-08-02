"""generic_chat 历史消息归一化 helper。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from hosts.web.app_support.llm_protocol import NormalizedMessage


def normalize_generic_history(
    history: list[Any],
    *,
    session_id: str | None = None,
) -> list[NormalizedMessage]:
    """把 Runner session history 转成前端 generic_chat 时间线消息。"""

    messages: list[NormalizedMessage] = []
    for msg_index, msg in enumerate(history):
        role = _extract_field(msg, "role", default="user")
        content = _extract_field(msg, "content", default="")
        tool_call_id = _extract_field(msg, "tool_call_id", default=None)
        if role not in ("user", "assistant", "tool"):
            role = "assistant"
        if not isinstance(content, str):
            content = ""
        timestamp = _message_timestamp(msg)
        metadata = _message_metadata(msg)

        tool_calls = _extract_field(msg, "tool_calls", default=None)
        if role == "assistant" and isinstance(tool_calls, (list, tuple)) and tool_calls:
            if content:
                assistant_text_message: NormalizedMessage = {
                    "id": _history_message_id(
                        session_id=session_id,
                        index=msg_index,
                        frame_type="text",
                    ),
                    "sessionId": session_id,
                    "timestamp": timestamp,
                    "provider": "generic_chat",
                    "frame_type": "text",
                    "role": "assistant",
                    "content": content,
                }
                if metadata:
                    assistant_text_message["metadata"] = metadata
                messages.append(assistant_text_message)
            for call_index, call in enumerate(tool_calls):
                call_id = _extract_field(call, "call_id", default=None)
                tool_name = _extract_field(call, "tool_name", default=None)
                arguments = _extract_field(call, "arguments", default=None)
                fallback_tool_id = _history_message_id(
                    session_id=session_id,
                    index=msg_index,
                    frame_type=f"tool-use-{call_index}",
                )
                messages.append(
                    {
                        "id": fallback_tool_id,
                        "sessionId": session_id,
                        "timestamp": timestamp,
                        "provider": "generic_chat",
                        "frame_type": "tool_use",
                        "toolId": call_id if isinstance(call_id, str) else fallback_tool_id,
                        "toolName": tool_name if isinstance(tool_name, str) else "unknown",
                        "toolInput": arguments if isinstance(arguments, dict) else {},
                    }
                )
            continue

        if role == "tool":
            raw_name = _extract_field(msg, "name", default=None)
            tool_name = raw_name if isinstance(raw_name, str) else None
            metadata = _extract_field(msg, "metadata", default=None)
            ok: bool | None = None
            error_message: str | None = None
            if isinstance(metadata, dict):
                meta_ok = metadata.get("ok")
                ok = bool(meta_ok) if isinstance(meta_ok, bool) else None
                meta_err = metadata.get("error_message")
                error_message = meta_err if isinstance(meta_err, str) else None
            messages.append(
                {
                    "id": _history_message_id(
                        session_id=session_id,
                        index=msg_index,
                        frame_type="tool-result",
                    ),
                    "sessionId": session_id,
                    "timestamp": timestamp,
                    "provider": "generic_chat",
                    "frame_type": "tool_result",
                    "toolId": tool_call_id
                    if isinstance(tool_call_id, str)
                    else _history_message_id(
                        session_id=session_id,
                        index=msg_index,
                        frame_type="tool-result-tool",
                    ),
                    "toolName": tool_name if isinstance(tool_name, str) else "unknown",
                    "content": content or error_message or "",
                    "isError": ok is False,
                }
            )
            continue

        out_role: Literal["user", "assistant"] = "assistant"
        if role == "user":
            out_role = "user"
        text_message: NormalizedMessage = {
            "id": _history_message_id(
                session_id=session_id,
                index=msg_index,
                frame_type="text",
            ),
            "sessionId": session_id,
            "timestamp": timestamp,
            "provider": "generic_chat",
            "frame_type": "text",
            "role": out_role,
            "content": content,
        }
        if metadata:
            text_message["metadata"] = metadata
        if out_role == "assistant":
            text_message["historyIndex"] = msg_index
        messages.append(text_message)
    return messages


def _extract_field(obj: Any, name: str, *, default: Any) -> Any:
    """读取 dict 或属性对象上的字段。"""

    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _message_timestamp(msg: Any) -> str:
    """优先复用历史消息自带时间；缺失时使用当前 UTC 时间。"""

    for field_name in ("timestamp", "created_at", "createdAt"):
        value = _extract_field(msg, field_name, default=None)
        if isinstance(value, str) and value:
            return value
    return _now_iso_utc()


def _message_metadata(msg: Any) -> dict[str, Any]:
    """读取历史消息 metadata；非 dict 或空 dict 时返回空。"""

    metadata = _extract_field(msg, "metadata", default=None)
    if not isinstance(metadata, dict) or not metadata:
        return {}
    return dict(metadata)


def _history_message_id(
    *,
    session_id: str | None,
    index: int,
    frame_type: str,
) -> str:
    """生成同一 session history 反复加载时保持稳定的前端消息 id。"""

    session_part = session_id or "generic-history"
    return f"{session_part}:{index}:{frame_type}"


def _now_iso_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["normalize_generic_history"]
