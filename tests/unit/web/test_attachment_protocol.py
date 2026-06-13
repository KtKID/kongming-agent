"""协议往返一致性测试：claude-image-paste-e2e §1 attachment-contract。

覆盖范围：
- ``UserInputAttachment`` pydantic round-trip（model_dump_json ↔ model_validate_json）
- ``UserInputFrame.attachments`` 含 / 不含两种情况
- ``kind`` / ``status`` Literal 字段的合法值与拒绝行为
- ``AttachmentAsset`` dataclass 字段完整性
- ``Message.metadata["attachments"]`` 字段名约定（核心约束：字段名锁定 ``attachments``）
- Python ↔ TS 字段对齐（硬编码 TS interface 字段集合，与 Python pydantic 字段集合比对）

设计要点：
- 字段对齐测试用 ``set(Model.model_fields.keys()) == EXPECTED_FIELDS`` 锁形状
- TS 字段集合写死在测试常量里——TS 改字段时本测试 fail，强制双侧同步
- 测试不发起任何 HTTP / WS 调用，纯协议层
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from core.message import Message
from hosts.web.protocol import UserInputAttachment
from hosts.web.protocol.ws_frames import UserInputFrame
from hosts.web.uploads.storage import AttachmentAsset, AttachmentKind, AttachmentStatus

# ---------------------------------------------------------------------------
# TS 真源（与 web/src/protocol.ts::UserInputAttachment interface 字段一致）
# ---------------------------------------------------------------------------

#: 与 ``web/src/protocol.ts::UserInputAttachment`` interface 字段集合一对一。
#: TS 侧改字段时本常量必须同步更新（双侧契约绑死）。
TS_USER_INPUT_ATTACHMENT_FIELDS: frozenset[str] = frozenset(
    {
        "asset_id",
        "kind",
        "mime_type",
        "size_bytes",
        "width",
        "height",
        "duration_ms",
        "preview_url",
        "status",
    }
)

#: 与 README §1 数据结构段 ``AttachmentAsset`` dataclass 字段集合一对一。
EXPECTED_ATTACHMENT_ASSET_FIELDS: frozenset[str] = frozenset(
    {
        "asset_id",
        "thread_id",
        "kind",
        "mime_type",
        "size_bytes",
        "width",
        "height",
        "duration_ms",
        "sha256",
        "storage_path",
        "preview_url",
        "status",
        "created_at",
    }
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_attachment(**overrides: object) -> UserInputAttachment:
    """构造一个合法的 ``UserInputAttachment`` 实例，可覆盖个别字段。"""
    payload: dict[str, object] = {
        "asset_id": "asset-001",
        "kind": "image",
        "mime_type": "image/png",
        "size_bytes": 1024,
        "width": 100,
        "height": 80,
        "duration_ms": None,
        "preview_url": "/api/uploads/asset-001",
        "status": "ready",
    }
    payload.update(overrides)
    return UserInputAttachment(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# §1.1 字段集合对齐（Python ↔ TS）
# ---------------------------------------------------------------------------


class TestFieldAlignment:
    """锁住 Python pydantic 字段与 TS interface 字段一一对应。"""

    def test_user_input_attachment_fields_match_ts(self) -> None:
        python_fields = frozenset(UserInputAttachment.model_fields.keys())
        assert python_fields == TS_USER_INPUT_ATTACHMENT_FIELDS, (
            f"Python ↔ TS 字段不一致；缺 TS: "
            f"{TS_USER_INPUT_ATTACHMENT_FIELDS - python_fields}；"
            f"多 Python: {python_fields - TS_USER_INPUT_ATTACHMENT_FIELDS}"
        )

    def test_attachment_asset_fields_match_spec(self) -> None:
        actual = frozenset(f.name for f in dataclasses.fields(AttachmentAsset))
        assert actual == EXPECTED_ATTACHMENT_ASSET_FIELDS, (
            f"AttachmentAsset 字段与 README §1 不一致；"
            f"缺: {EXPECTED_ATTACHMENT_ASSET_FIELDS - actual}；"
            f"多: {actual - EXPECTED_ATTACHMENT_ASSET_FIELDS}"
        )

    def test_user_input_attachment_is_superset_of_asset_minus_server_fields(
        self,
    ) -> None:
        """WS 协议字段是 AttachmentAsset 子集（去掉服务端独有字段）。

        服务端独有：thread_id / sha256 / storage_path / created_at —— 这些
        不通过 WS 暴露给前端。
        """
        ts_fields = TS_USER_INPUT_ATTACHMENT_FIELDS
        asset_fields = EXPECTED_ATTACHMENT_ASSET_FIELDS
        server_only = {"thread_id", "sha256", "storage_path", "created_at"}
        assert ts_fields == asset_fields - server_only


# ---------------------------------------------------------------------------
# §1.2 pydantic round-trip
# ---------------------------------------------------------------------------


class TestUserInputAttachmentRoundTrip:
    def test_full_payload_round_trip(self) -> None:
        original = _make_attachment(width=200, height=150)
        restored = UserInputAttachment.model_validate_json(original.model_dump_json())
        assert restored == original

    def test_minimal_payload_round_trip(self) -> None:
        """只填必填字段，optional 字段默认 None。"""
        original = UserInputAttachment(
            asset_id="asset-002",
            kind="image",
            mime_type="image/jpeg",
            size_bytes=2048,
            preview_url="/api/uploads/asset-002",
            status="ready",
        )
        restored = UserInputAttachment.model_validate_json(original.model_dump_json())
        assert restored == original
        assert restored.width is None
        assert restored.height is None
        assert restored.duration_ms is None


# ---------------------------------------------------------------------------
# §1.3 Literal 字段校验
# ---------------------------------------------------------------------------


class TestLiteralValidation:
    @pytest.mark.parametrize("kind", ["image", "video", "file"])
    def test_kind_accepts_extensible_values(self, kind: str) -> None:
        att = _make_attachment(kind=kind)
        assert att.kind == kind

    @pytest.mark.parametrize("bad_kind", ["audio", "doc", "", "Image", "IMAGE"])
    def test_kind_rejects_invalid(self, bad_kind: str) -> None:
        with pytest.raises(ValidationError):
            _make_attachment(kind=bad_kind)

    @pytest.mark.parametrize("status", ["ready", "processing", "failed"])
    def test_status_accepts_three_states(self, status: str) -> None:
        att = _make_attachment(status=status)
        assert att.status == status

    @pytest.mark.parametrize("bad_status", ["pending", "done", "", "READY"])
    def test_status_rejects_invalid(self, bad_status: str) -> None:
        with pytest.raises(ValidationError):
            _make_attachment(status=bad_status)


# ---------------------------------------------------------------------------
# §1.4 UserInputFrame.attachments 升级（向后兼容）
# ---------------------------------------------------------------------------


class TestUserInputFrameAttachments:
    def test_frame_without_attachments_round_trip(self) -> None:
        """不带 attachments 字段时（旧客户端） attachments 默认 None。"""
        frame = UserInputFrame(text="hello", request_id="req-1")
        assert frame.attachments is None

        restored = UserInputFrame.model_validate_json(frame.model_dump_json())
        assert restored == frame
        assert restored.attachments is None

    def test_frame_with_empty_attachments_list(self) -> None:
        """显式 attachments=[] 与 None 在语义上等价但都允许。"""
        frame = UserInputFrame(text="hi", request_id="req-2", attachments=[])
        restored = UserInputFrame.model_validate_json(frame.model_dump_json())
        assert restored.attachments == []

    def test_frame_with_attachments_round_trip(self) -> None:
        att = _make_attachment()
        frame = UserInputFrame(
            text="see this image",
            request_id="req-3",
            attachments=[att],
        )
        restored = UserInputFrame.model_validate_json(frame.model_dump_json())
        assert restored == frame
        assert restored.attachments is not None
        assert len(restored.attachments) == 1
        assert restored.attachments[0].asset_id == "asset-001"

    def test_frame_with_multiple_attachments(self) -> None:
        atts = [_make_attachment(asset_id=f"asset-{i:03d}") for i in range(3)]
        frame = UserInputFrame(text="three pics", request_id="req-4", attachments=atts)
        restored = UserInputFrame.model_validate_json(frame.model_dump_json())
        assert restored.attachments is not None
        assert [a.asset_id for a in restored.attachments] == [
            "asset-000",
            "asset-001",
            "asset-002",
        ]


# ---------------------------------------------------------------------------
# §1.6 AttachmentAsset dataclass
# ---------------------------------------------------------------------------


class TestAttachmentAsset:
    def test_frozen_dataclass(self) -> None:
        """frozen=True 保证不可变（防意外修改资产元数据）。"""
        asset = AttachmentAsset(
            asset_id="a",
            thread_id="t",
            kind="image",
            mime_type="image/png",
            size_bytes=1,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            asset.size_bytes = 999  # type: ignore[misc]

    def test_default_values(self) -> None:
        asset = AttachmentAsset(
            asset_id="a",
            thread_id="t",
            kind="image",
            mime_type="image/png",
            size_bytes=1,
        )
        assert asset.width is None
        assert asset.height is None
        assert asset.duration_ms is None
        assert asset.sha256 == ""
        assert asset.storage_path == ""
        assert asset.preview_url == ""
        assert asset.status == "ready"
        assert asset.created_at == 0.0

    def test_kind_alias_matches_protocol(self) -> None:
        """AttachmentKind 与 UserInputAttachment.kind 取值集合一致。"""
        # Literal 取值集合通过 typing.get_args 反射
        from typing import get_args

        asset_kinds = set(get_args(AttachmentKind))
        protocol_kind_field = UserInputAttachment.model_fields["kind"]
        # pydantic v2: annotation 是 Literal["image","video","file"]
        protocol_kinds = set(get_args(protocol_kind_field.annotation))
        assert asset_kinds == protocol_kinds

    def test_status_alias_matches_protocol(self) -> None:
        from typing import get_args

        asset_statuses = set(get_args(AttachmentStatus))
        protocol_status_field = UserInputAttachment.model_fields["status"]
        protocol_statuses = set(get_args(protocol_status_field.annotation))
        assert asset_statuses == protocol_statuses


# ---------------------------------------------------------------------------
# §1.7 Message.metadata["attachments"] 字段名约定
# ---------------------------------------------------------------------------


class TestMessageMetadataAttachmentsConvention:
    """锁住 Message.metadata 上 attachments 的字段名约定（key=``attachments``）。

    Message.metadata 是 dict[str, Any]，类型系统无法约束 key 名；
    本测试通过断言 ``Message.user(..., metadata={"attachments": [...]})``
    能存取，把字段名 ``attachments`` 锁死。后续 §4 assembler / §5 adapter
    会读这个 key。
    """

    def test_user_message_stores_attachments_under_attachments_key(self) -> None:
        att_ref = {
            "asset_id": "asset-001",
            "kind": "image",
            "mime_type": "image/png",
            "size_bytes": 1024,
            "preview_url": "/api/uploads/asset-001",
            "status": "ready",
        }
        msg = Message.user(
            content="see this",
            metadata={"attachments": [att_ref]},
        )
        assert msg.metadata is not None
        assert "attachments" in msg.metadata
        assert msg.metadata["attachments"] == [att_ref]

    def test_user_message_without_attachments_metadata_works(self) -> None:
        """不带 attachments 时纯文本路径不破坏（向后兼容）。"""
        msg = Message.user(content="plain text")
        # metadata 可能是 None 或 dict 不含 attachments key —— 两种都算合规
        if msg.metadata is not None:
            assert "attachments" not in msg.metadata

    def test_attachments_metadata_round_trip_serializable(self) -> None:
        """metadata 必须是 JSON-serializable（jsonl 持久化需要）。"""
        import json

        att_ref = {
            "asset_id": "asset-002",
            "kind": "image",
            "mime_type": "image/jpeg",
            "size_bytes": 2048,
            "preview_url": "/api/uploads/asset-002",
            "status": "ready",
        }
        msg = Message.user(
            content="with img",
            metadata={"attachments": [att_ref]},
        )
        # 直接 json.dumps 不抛错即可
        assert msg.metadata is not None
        encoded = json.dumps(msg.metadata)
        decoded = json.loads(encoded)
        assert decoded["attachments"][0]["asset_id"] == "asset-002"
