"""protocol.py 单元测试。

覆盖：
- 5 类 SidecarRequest 各自的合法路径（含 camelCase 别名 + 可选字段）
- 顶层校验：JSON 非法 / 顶层非 object / protocolVersion 错 / type 不识别
- 字段层校验：缺必填字段 / 类型错
"""

from __future__ import annotations

import json

import pytest

from claude_sidecar.protocol import (
    InterruptRequest,
    ParsedRequest,
    ParseError,
    ReconnectRequest,
    ResolveApprovalRequest,
    ShutdownRequest,
    StartRequest,
    parse_request,
)

# --- 合法路径 -----------------------------------------------------------------


def test_start_full_payload() -> None:
    raw = json.dumps(
        {
            "protocolVersion": "1",
            "type": "start",
            "workspaceId": "ws_1",
            "threadId": "th_1",
            "runId": "run_1",
            "cwd": "/tmp/work",
            "prompt": "hello",
            "resume": "session_xxx",
            "model": "claude-sonnet-4-6",
            "permissionMode": "default",
            "allowedTools": ["Read", "Bash"],
            "disallowedTools": ["WebFetch"],
            "contextPackPath": "/tmp/ctx.md",
            "continuePointSnapshot": "...",
            "metadata": {"foo": "bar"},
        }
    )
    parsed = parse_request(raw)
    assert isinstance(parsed, ParsedRequest)
    req = parsed.request
    assert isinstance(req, StartRequest)
    assert req.workspace_id == "ws_1"
    assert req.thread_id == "th_1"
    assert req.run_id == "run_1"
    assert req.cwd == "/tmp/work"
    assert req.prompt == "hello"
    assert req.resume == "session_xxx"
    assert req.model == "claude-sonnet-4-6"
    assert req.permission_mode == "default"
    assert req.allowed_tools == ["Read", "Bash"]
    assert req.disallowed_tools == ["WebFetch"]
    assert req.context_pack_path == "/tmp/ctx.md"
    assert req.continue_point_snapshot == "..."
    assert req.metadata == {"foo": "bar"}


def test_start_minimal_payload() -> None:
    """只有必填字段，可选字段全空。"""
    raw = json.dumps(
        {
            "protocolVersion": "1",
            "type": "start",
            "workspaceId": "ws",
            "threadId": "th",
            "runId": "run",
            "cwd": "/",
            "prompt": "hi",
        }
    )
    parsed = parse_request(raw)
    assert isinstance(parsed, ParsedRequest)
    assert isinstance(parsed.request, StartRequest)
    assert parsed.request.resume is None
    assert parsed.request.metadata is None


def test_resolve_approval_allow() -> None:
    raw = json.dumps(
        {
            "protocolVersion": "1",
            "type": "resolve_approval",
            "threadId": "th",
            "requestId": "req_1",
            "approved": True,
            "updatedInput": {"command": "ls -la"},
            "rememberEntry": "Bash(ls:*)",
        }
    )
    parsed = parse_request(raw)
    assert isinstance(parsed, ParsedRequest)
    req = parsed.request
    assert isinstance(req, ResolveApprovalRequest)
    assert req.approved is True
    assert req.updated_input == {"command": "ls -la"}
    assert req.remember_entry == "Bash(ls:*)"


def test_resolve_approval_deny() -> None:
    raw = json.dumps(
        {
            "protocolVersion": "1",
            "type": "resolve_approval",
            "threadId": "th",
            "requestId": "req_1",
            "approved": False,
            "message": "user denied",
        }
    )
    parsed = parse_request(raw)
    assert isinstance(parsed, ParsedRequest)
    req = parsed.request
    assert isinstance(req, ResolveApprovalRequest)
    assert req.approved is False
    assert req.message == "user denied"


def test_interrupt() -> None:
    raw = json.dumps({"protocolVersion": "1", "type": "interrupt", "threadId": "th"})
    parsed = parse_request(raw)
    assert isinstance(parsed, ParsedRequest)
    assert isinstance(parsed.request, InterruptRequest)
    assert parsed.request.thread_id == "th"


def test_reconnect() -> None:
    raw = json.dumps(
        {
            "protocolVersion": "1",
            "type": "reconnect",
            "workspaceId": "ws",
            "threadId": "th",
            "runId": "run",
            "runtimeSessionId": "sess_xxx",
        }
    )
    parsed = parse_request(raw)
    assert isinstance(parsed, ParsedRequest)
    req = parsed.request
    assert isinstance(req, ReconnectRequest)
    assert req.runtime_session_id == "sess_xxx"


def test_shutdown_with_reason() -> None:
    raw = json.dumps(
        {
            "protocolVersion": "1",
            "type": "shutdown",
            "threadId": "th",
            "reason": "user_quit",
        }
    )
    parsed = parse_request(raw)
    assert isinstance(parsed, ParsedRequest)
    req = parsed.request
    assert isinstance(req, ShutdownRequest)
    assert req.reason == "user_quit"


def test_shutdown_without_reason() -> None:
    raw = json.dumps({"protocolVersion": "1", "type": "shutdown", "threadId": "th"})
    parsed = parse_request(raw)
    assert isinstance(parsed, ParsedRequest)
    assert isinstance(parsed.request, ShutdownRequest)
    assert parsed.request.reason is None


# --- 顶层校验失败 -------------------------------------------------------------


def test_invalid_json() -> None:
    parsed = parse_request("{not json")
    assert isinstance(parsed, ParseError)
    assert parsed.reason == "json_decode"


def test_top_level_array_not_object() -> None:
    parsed = parse_request("[1, 2, 3]")
    assert isinstance(parsed, ParseError)
    assert parsed.reason == "schema"
    assert "object" in parsed.detail.lower()


def test_top_level_string() -> None:
    parsed = parse_request('"hello"')
    assert isinstance(parsed, ParseError)
    assert parsed.reason == "schema"


@pytest.mark.parametrize(
    "bad_pv",
    [None, "2", 1, "v1", ""],
)
def test_protocol_version_must_be_string_one(bad_pv: object) -> None:
    payload: dict[str, object] = {
        "type": "interrupt",
        "threadId": "th",
    }
    if bad_pv is not None:
        payload["protocolVersion"] = bad_pv
    parsed = parse_request(json.dumps(payload))
    assert isinstance(parsed, ParseError)
    assert parsed.reason == "protocol_version"


def test_unknown_type() -> None:
    raw = json.dumps({"protocolVersion": "1", "type": "frobnicate", "threadId": "th"})
    parsed = parse_request(raw)
    assert isinstance(parsed, ParseError)
    assert parsed.reason == "unknown_type"


def test_type_field_missing() -> None:
    raw = json.dumps({"protocolVersion": "1", "threadId": "th"})
    parsed = parse_request(raw)
    assert isinstance(parsed, ParseError)
    assert parsed.reason == "unknown_type"


# --- 字段级校验失败 -----------------------------------------------------------


def test_start_missing_required_field() -> None:
    """缺 cwd 字段。"""
    raw = json.dumps(
        {
            "protocolVersion": "1",
            "type": "start",
            "workspaceId": "ws",
            "threadId": "th",
            "runId": "run",
            "prompt": "hi",
        }
    )
    parsed = parse_request(raw)
    assert isinstance(parsed, ParseError)
    assert parsed.reason == "schema"
    assert "cwd" in parsed.detail


def test_resolve_approval_approved_must_be_bool() -> None:
    raw = json.dumps(
        {
            "protocolVersion": "1",
            "type": "resolve_approval",
            "threadId": "th",
            "requestId": "r",
            "approved": "yes",
        }
    )
    parsed = parse_request(raw)
    assert isinstance(parsed, ParseError)
    assert parsed.reason == "schema"


def test_extra_fields_ignored() -> None:
    """协议外字段应该被默默忽略（extra='ignore'），不影响解析。"""
    raw = json.dumps(
        {
            "protocolVersion": "1",
            "type": "interrupt",
            "threadId": "th",
            "future_field_v2": "hello",
        }
    )
    parsed = parse_request(raw)
    assert isinstance(parsed, ParsedRequest)
