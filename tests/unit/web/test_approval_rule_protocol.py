"""审批 rememberRule Python/TypeScript 双侧协议合同。"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from pydantic import ValidationError


def _protocol() -> object:
    return importlib.import_module("hosts.web.protocol")


def _item_payload() -> dict[str, object]:
    return {
        "requestId": "req-a",
        "threadId": "thread-a",
        "toolName": "run_shell",
        "toolInput": {"command": "ls -la"},
        "autoApproveAtMs": None,
        "blockedByRule": None,
        "isElevated": False,
        "danger": False,
        "rememberAllowed": True,
        "channel": "generic_chat",
        "cwd": "/workspace/a",
        "arrivedAtMs": 1000,
        "timeoutMs": None,
        "autoRejectAtMs": None,
        "rememberRule": {
            "expression": "run_shell(ls:*)",
            "displayText": "允许 ls 开头的命令",
            "scopeCwd": "/workspace/a",
        },
    }


def test_remember_rule_model_is_frozen_and_forbids_unknown_fields() -> None:
    protocol = _protocol()
    try:
        remember_cls = protocol.RememberRule
    except AttributeError as exc:
        pytest.fail(f"RememberRule Python 协议尚未实现: {exc}", pytrace=False)
    remember = remember_cls(
        expression="run_shell(ls:*)",
        displayText="允许 ls 开头的命令",
        scopeCwd="/workspace/a",
    )
    assert remember.model_dump() == {
        "expression": "run_shell(ls:*)",
        "displayText": "允许 ls 开头的命令",
        "scopeCwd": "/workspace/a",
    }
    with pytest.raises(ValidationError):
        remember_cls(
            expression="run_shell(ls:*)",
            displayText="允许 ls 开头的命令",
            scopeCwd="/workspace/a",
            extraField=True,
        )


def test_pending_item_requires_remember_rule_field_with_nullable_value() -> None:
    protocol = _protocol()
    item = protocol.ApprovalInboxItem.model_validate(_item_payload())
    assert item.rememberRule.expression == "run_shell(ls:*)"
    nullable = {**_item_payload(), "rememberRule": None}
    assert protocol.ApprovalInboxItem.model_validate(nullable).rememberRule is None
    missing = _item_payload()
    missing.pop("rememberRule")
    with pytest.raises(ValidationError):
        protocol.ApprovalInboxItem.model_validate(missing)


def test_pending_add_round_trip_keeps_exact_remember_rule() -> None:
    protocol = _protocol()
    payload = {"frame_type": "approval.inbox.add", **_item_payload()}
    frame = protocol.ApprovalInboxAddFrame.model_validate(payload)
    assert frame.model_dump(mode="json") == payload


def test_resolve_frame_accepts_same_remember_rule_and_scope() -> None:
    protocol = _protocol()
    frame = protocol.ApprovalInboxResolveFrame.model_validate(
        {
            "frame_type": "approval.inbox.resolve",
            "threadId": "thread-a",
            "requestId": "req-a",
            "allow": True,
            "remember": True,
            "rememberRule": _item_payload()["rememberRule"],
        }
    )
    dumped = frame.model_dump(mode="json", exclude_none=True)
    assert dumped["rememberRule"] == _item_payload()["rememberRule"]
    assert dumped["remember"] is True


def test_resolve_once_allows_missing_remember_rule() -> None:
    protocol = _protocol()
    frame = protocol.ApprovalInboxResolveFrame.model_validate(
        {
            "frame_type": "approval.inbox.resolve",
            "threadId": "thread-a",
            "requestId": "req-a",
            "allow": True,
        }
    )
    assert frame.rememberRule is None
    assert frame.remember is False


def test_resolve_remember_rejects_missing_rule_claim() -> None:
    """remember=true 时协议层拒绝缺失冻结候选。"""
    protocol = _protocol()
    with pytest.raises(ValidationError, match="rememberRule is required"):
        protocol.ApprovalInboxResolveFrame.model_validate(
            {
                "frame_type": "approval.inbox.resolve",
                "threadId": "thread-a",
                "requestId": "req-a",
                "allow": True,
                "remember": True,
            }
        )


def test_resolve_result_reports_backend_acceptance_and_error() -> None:
    protocol = _protocol()
    frame = protocol.ApprovalInboxResolveResultFrame.model_validate(
        {
            "frame_type": "approval.inbox.resolve_result",
            "requestId": "req-a",
            "accepted": False,
            "message": "规则保存失败，请重试",
        }
    )
    assert frame.model_dump(mode="json") == {
        "frame_type": "approval.inbox.resolve_result",
        "requestId": "req-a",
        "accepted": False,
        "message": "规则保存失败，请重试",
    }


def test_resolve_frame_rejects_legacy_remember_entry() -> None:
    protocol = _protocol()
    with pytest.raises(ValidationError):
        protocol.ApprovalInboxResolveFrame.model_validate(
            {
                "frame_type": "approval.inbox.resolve",
                "threadId": "thread-a",
                "requestId": "req-a",
                "allow": True,
                "rememberEntry": "Bash",
            }
        )


def test_typescript_protocol_matches_python_remember_rule_shape() -> None:
    source = Path("web/src/protocol/ws-thread-status.ts").read_text(encoding="utf-8")
    assert "export interface RememberRule" in source
    assert "expression: string;" in source
    assert "displayText: string;" in source
    assert "scopeCwd: string | null;" in source
    assert "rememberRule: RememberRule | null;" in source
    assert "rememberRule?: RememberRule | null;" in source
    assert "rememberScope" not in source
    assert "export interface ApprovalInboxResolveResultFrame" in source
    assert "rememberEntry" not in source


def test_protocol_barrel_exports_remember_rule() -> None:
    source = Path("web/src/protocol.ts").read_text(encoding="utf-8")
    export_block = source[source.index("export type {") :]
    assert "RememberRule," in export_block


def test_permission_rule_dto_is_strict_and_typescript_shape_matches() -> None:
    """结构化 REST 规则固定 expression、nullable scope 与 schema v2。"""
    protocol = _protocol()
    rule = protocol.PermissionRuleDTO.model_validate(
        {"expression": "run_shell(git:*)", "scope_cwd": "/repo/a"}
    )
    assert rule.model_dump() == {
        "expression": "run_shell(git:*)",
        "scope_cwd": "/repo/a",
    }
    with pytest.raises(ValidationError):
        protocol.PermissionRuleDTO.model_validate(
            {
                "expression": "run_shell(git:*)",
                "scope_cwd": "/repo/a",
                "scope": "workspace",
            }
        )
    with pytest.raises(ValidationError):
        protocol.PermissionRuleDTO.model_validate({"expression": "run_shell(git:*)"})

    source = Path("web/src/protocol.ts").read_text(encoding="utf-8")
    assert "export interface PermissionRuleDTO" in source
    assert "scope_cwd: string | null;" in source
    assert "schema_version: 2;" in source
    assert "allow: PermissionRuleDTO[];" in source
